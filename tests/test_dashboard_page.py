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
    srv = dashboard.DashboardServer(("127.0.0.1", 0), dashboard.Handler)
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
        "conv",
        "rail",
        "content",
        "chatinput",
        "chatsend",
        "chatstatus",
        "live",
        "langsel",
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
    # Centralized, immutable proc_state -> {key, tone} table; statusOf only
    # applies the dead latest-task Failed/Done override on top of it. Labels
    # resolve through the LOCALES dictionary at render time, so the map holds
    # stable message keys, not baked English.
    m = re.search(r"const PROC_STATUS=Object\.freeze\(\{([\s\S]*?)\}\)", PAGE)
    assert m, "PROC_STATUS freeze map missing"
    body = m.group(1)
    for entry in (
        'busy:{key:"status.proc.running",tone:"running"}',
        'spawning:{key:"status.proc.starting",tone:"running"}',
        'ready:{key:"status.proc.ready",tone:"neutral"}',
        'idle_unloaded:{key:"status.proc.idle",tone:"neutral"}',
    ):
        assert entry in body
    assert '"Waiting"' not in PAGE
    assert "PROC_STATUS[st]" in PAGE
    dead = re.search(r'if\(st==="dead"\)\{([\s\S]*?)\n  \}', PAGE).group(1)
    assert "latestTask" in dead and '"failed"' in dead
    assert 'key:"status.proc.failed",tone:"error"' in dead
    assert 'key:"status.proc.done",tone:"success"' in dead
    # Unknown proc_states surface a localized Unknown plus the raw code.
    assert '"status.proc.unknown"' in PAGE and "raw:" in PAGE
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
    body = re.search(r"function taskDur\(tk\)\{([\s\S]*?)\n\}", PAGE).group(1)
    # The clock starts strictly at a valid started_at — never created_at,
    # queue time, or session age.
    assert "Date.parse(tk.started_at)" in body
    assert "created_at" not in body and "last_active_at" not in body
    # Only queued/running may be live; terminal statuses require finished_at.
    assert 'tk.status==="queued"||tk.status==="running"' in body
    assert "Date.parse(tk.finished_at)" in body
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


def test_session_header_identifiers():
    """The right-pane title names the displayed sub-agent's identifiers.

    The Agent Bridge task_id (the session's latest task row) and the
    session_id render inside the h2 title — resolved per selection, escaped
    as data, labeled via the bundled dictionaries, and dropped rather than
    stale when absent. Executable end-to-end coverage lives in
    tests/dashboard_status_behavior.js.
    """
    body = re.search(r"function renderSessionHeader\(\)\{([\s\S]*?)\n\}", PAGE)
    assert body, "renderSessionHeader missing"
    body = body.group(1)
    # task_id comes from the selected session's own latest task — never a
    # global or foreign row.
    assert "latestTask(s.session_id)" in body
    for needle in ('"session.task_id"', '"session.session_id"', "esc(ids)"):
        assert needle in body
    # Each id renders only when present: the join drops the missing part
    # instead of printing "undefined"/"null" or a stale value.
    assert "filter(Boolean)" in body
    # The pair lives inside the h2 after the still-primary title text.
    h2 = re.search(r'<h2 class="htitle">([\s\S]*?)</h2>', body)
    assert h2, "htitle markup missing"
    frag = h2.group(1)
    assert 'class="htext"' in frag and 'class="hids"' in frag
    assert frag.index('class="htext"') < frag.index('class="hids"')
    assert "esc(s.title||s.session_id)" in frag
    # The full untruncated pair stays reachable via tooltip.
    assert 'title="${esc(ids)}"' in frag
    # The labels resolve in every bundled dictionary (id text stays data).
    for loc in _locales().values():
        assert "session.task_id" in loc and "session.session_id" in loc
    # Layout: the title text ellipsizes first; the ids span stays put.
    htitle = re.search(r"\.htitle\{([^}]*)\}", PAGE).group(1)
    assert "display:flex" in htitle
    htext = re.search(r"\.htext\{([^}]*)\}", PAGE).group(1)
    assert "min-width:0" in htext and "text-overflow:ellipsis" in htext
    hids = re.search(r"\.hids\{([^}]*)\}", PAGE).group(1)
    assert "flex:none" in hids and "var(--font-mono)" in hids


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
    # The label itself comes from the dictionary (transcript.turn_ended).
    assert 'parts=[t("transcript.turn_ended")]' in body
    assert "stopReasonLabel" in body
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
        '"tokens.unit"',
        '"tokens.last_snapshot"',
    ):
        assert needle in PAGE
    # The sidebar signature fingerprints run_usage (preferred), legacy usage,
    # and the live usage-event snapshot so a finishing/running turn repaints.
    sig = re.search(r'const sig=([\s\S]*?)join\("\|"\)', PAGE).group(1)
    assert "tk.run_usage" in sig and "tk.usage" in sig and "liveUsage" in sig
    # tokSpan prefers the persisted per-run aggregate over the raw last
    # snapshot; legacy usage is only an estimate fallback.
    body = re.search(r"function tokSpan\(tk,cls,live\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "tk.run_usage" in body and "runTok" in body
    assert 'quality:"estimate"' in body
    # The legacy fallback is labeled a last-snapshot estimate, not a run total.
    title = re.search(r"function tokTitle\(o\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert 't("tokens.last_snapshot"' in title and "o.legacy" in title
    # The compact-count unit comes from the dictionary, not a hardcoded suffix.
    assert 't("tokens.unit")' in body
    # Usage text stays static between overview polls — not joined to the tick.
    tick = re.search(r"function tickDurations\(\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "stok" not in tick and "htok" not in tick


def test_output_tokens_per_second():
    """Header-only output tok/s: output tokens over elapsed task seconds —
    live while queued/running, frozen at finished_at for terminal tasks."""
    for needle in (
        "function calcTps(",
        "function tpsSpan(",
        '"htps"',
        '"tokens.per_sec"',
        '"tokens.out_speed"',
    ):
        assert needle in PAGE
    # Same tabular-numeral styling rule as the other inline metrics.
    assert re.search(r"\.sdur,\.hdur,\.stok,\.htok,\.htps\{", PAGE)
    calc = re.search(r"function calcTps\(tk,live\)\{([\s\S]*?)\n\}", PAGE)
    assert calc, "calcTps helper missing"
    body = calc.group(1)
    # Output tokens only — never the input or the consumed total.
    assert "info.output" in body and "usageNums" in body
    # The live consumed snapshot only applies to queued/running; the
    # persisted run_usage and the legacy raw usage mirror tokSpan precedence.
    assert 'tk.status==="running"||tk.status==="queued"' in body
    assert "tk.run_usage" in body and "tk.usage" in body
    # Elapsed interval is taskDur's started_at->finished_at clock (Date.now()
    # while active), so terminal rates freeze and inverted/sub-second runs
    # are suppressed instead of spiking.
    assert "taskDur(tk)" in body and "Date.now()" in body and "sec<1" in body
    span = re.search(r"function tpsSpan\(tk,cls,live\)\{([\s\S]*?)\n\}", PAGE)
    assert span, "tpsSpan helper missing"
    sbody = span.group(1)
    # >=10 renders as an integer, lower positive rates keep one decimal; the
    # tooltip doubles as the aria-label.
    assert ">=10" in sbody and "toFixed(1)" in sbody
    assert 't("tokens.per_sec"' in sbody and 't("tokens.out_speed"' in sbody
    assert "aria-label" in sbody
    # Placement: inside .hstatus directly after the existing .htok span —
    # never the sidebar's .sstatus, never the .htitle line.
    hstatus = next(
        line for line in PAGE.splitlines() if 'class="hstatus"' in line)
    assert 'tokSpan(tk,"htok"' in hstatus and 'tpsSpan(tk,"htps"' in hstatus
    assert hstatus.index('tokSpan(tk,"htok"') < hstatus.index('tpsSpan(tk,"htps"')
    sstatus = next(
        line for line in PAGE.splitlines() if 'class="sstatus"' in line)
    assert "tpsSpan" not in sstatus
    htitle = next(
        line for line in PAGE.splitlines() if 'class="htitle"' in line)
    assert "tpsSpan" not in htitle and "htps" not in htitle
    # Poll/event-driven updates only — the 1s duration tick never churns it.
    tick = re.search(r"function tickDurations\(\)\{([\s\S]*?)\n\}", PAGE).group(1)
    for gone in ("tpsSpan", "calcTps", "htps"):
        assert gone not in tick
    # Localized in all three dictionaries with the documented placeholders.
    locales = _locales()
    for loc in ("en", "zh-CN", "zh-TW"):
        d = locales[loc]
        assert "{n}" in d["tokens.per_sec"]
        for ph in ("{rate}", "{out}", "{dur}"):
            assert ph in d["tokens.out_speed"], f"{ph} missing in {loc}"


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


def test_nav_rail():
    # Voyager-style timeline ruler: a semantic landmark right of the
    # scroller with one fixed-length tick per conversation beat.
    rail_nav = re.search(r'<nav id="rail"[^>]*>', PAGE).group(0)
    assert 'aria-label="Message positions"' in rail_nav and "hidden" in rail_nav
    # The landmark label localizes via the i18n hook.
    assert 'data-i18n-aria-label="rail.label"' in rail_nav
    # The landmark label must not redundantly contain the role name.
    assert "Message positions" in PAGE and "navigation" not in rail_nav.lower()
    # The rail renders to the RIGHT of the scroller inside #conv.
    conv = re.search(r'<div id="conv">([\s\S]*?)</div>\s*<div id="chatbar">', PAGE)
    assert conv and conv.group(1).index('id="content"') < conv.group(1).index('id="rail"')
    # Marks cover agent cards, non-user Dispatched cards and turn ends —
    # error dividers (.turnend.err) are not turn ends. MARK_SEL is the single
    # source of truth: layoutRail applies it via querySelectorAll and the JS
    # behavior tests + benchmark consume the same literal — no second
    # taxonomy copy exists to drift.
    sel = re.search(r'const MARK_SEL="([^"]+)"', PAGE)
    assert sel, "MARK_SEL selector missing"
    assert sel.group(1) == ".block.card.msg,.block.card.prompt:not(.user),.turnend:not(.err)"
    layout = re.search(r"function layoutRail\(\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "querySelectorAll(MARK_SEL)" in layout
    # Compact centered geometry (Voyager buildCompactMarkerOffsets): one tick
    # per markable element, evenly spaced around the rail midpoint with
    # step=min(8px,160px/(n-1)) — never a document-fraction map, never a
    # cluster of merged sources.
    assert "Math.min(8,160/Math.max(1,n-1))" in layout
    assert "(i-(n-1)/2)*step" in layout
    assert "clusterYs" not in PAGE and "MARK_GAP" not in PAGE
    assert "offsetTop/doc" not in PAGE
    # No proportional bare-track seek: compact offsets carry no document
    # position, so the rail registers no click-to-seek listener at all.
    assert 'rail.addEventListener("click"' not in PAGE
    # The measured scrollbar gutter (--railin) insets the overlaid rail from
    # the pane's right edge, keeping it immediately LEFT of the native
    # scrollbar; offsetWidth-clientWidth is the platform-agnostic measure.
    assert "offsetWidth" in layout and "clientWidth" in layout
    assert 'pane.style.setProperty("--railin"' in layout
    # Layout, wave and interaction machinery.
    for needle in (
        "markRank",
        "scheduleRail",
        "layoutRail",
        "jumpToMark",
        "updateCurMark",
        "updateRulerWave",
        "railBtns",
        "markVars",
        "setProperty",
        "requestAnimationFrame",
        "MutationObserver",
        "ResizeObserver",
        "attributeFilter",
        "childList:true",
        "subtree:true",
        "characterData:true",
        "aria-current",
        "aria-label",
        "tabIndex",
        '"ArrowDown"',
        '"ArrowUp"',
        '"Home"',
        '"End"',
        '"wheel"',
        "scrollTo",
        "scrollTop",
        "prefers-reduced-motion",
        "rail.hidden",
    ):
        assert needle in PAGE
    # The rail is an absolute overlay inside #conv — not a flex sibling — so
    # #conv must be its positioned containing block.
    assert re.search(r"#conv\{[^}]*position:relative[^}]*display:flex", PAGE)
    rail_css = re.search(r"#rail\{([^}]*)\}", PAGE).group(1)
    assert "position:absolute" in rail_css
    assert "right:var(--railin)" in rail_css and "width:var(--railw)" in rail_css
    # Voyager ruler mode: no full-height spine behind the ticks.
    assert "#rail::before" not in PAGE
    # #content keeps a stable native-scrollbar gutter and symmetric padding,
    # so the overlay can never cover card text.
    content_css = re.search(r"#content\{([^}]*)\}", PAGE).group(1)
    assert "scrollbar-gutter:stable" in content_css
    assert "overflow-y:auto" in content_css and "position:relative" in content_css
    assert "padding:20px 26px 24px" in content_css
    # Every graduation is the same fixed 14px length; only thickness encodes
    # kind (2/3/4px) — no scaleX or per-tick length variation anywhere.
    mark_css = re.search(r"\.mark\{([^}]*)\}", PAGE).group(1)
    assert "width:14px" in mark_css and "height:2px" in mark_css
    assert "var(--dimmer)" in mark_css and "cursor:pointer" in mark_css
    assert "scaleX" not in PAGE
    assert re.search(r"\.mark\.k-disp\{[^}]*height:3px", PAGE)
    assert re.search(r"\.mark\.k-turn\{[^}]*height:4px", PAGE)
    assert re.search(r"\.mark\.cur\{[^}]*var\(--accent\)", PAGE)
    assert re.search(r"\.mark:focus-visible\{[^}]*outline:2px", PAGE)
    narrow = re.search(r"@media \(max-width:900px\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "#pane{--railw:14px}" in narrow
    assert "calc(16px + var(--railin))" in narrow
    # Scroll and session-switch hooks keep the current mark and wave live.
    scroll = re.search(r'content\.addEventListener\("scroll",\(\)=>\{([\s\S]*?)\}\)', PAGE)
    assert "updateCurMark" in scroll.group(1) and "updateRulerWave" in scroll.group(1)
    sel_body = re.search(r"function select\(id\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "scheduleRail()" in sel_body
    # Mark aria-labels/tooltips come from the dictionary, keyed by card kind —
    # one tick per source, so no "N messages" cluster label exists.
    assert '"rail.agent_message"' in PAGE
    assert '"rail.dispatched_message"' in PAGE
    assert '"rail.turn_end"' in PAGE
    assert "MARK_LBL[rank]" in PAGE
    assert 't("rail.messages"' not in PAGE
    assert '"rail.messages"' not in PAGE


def test_centered_content_axis():
    # Every right-pane inner column centers on one axis — the scrollbar-free
    # viewport center — with the measured gutter reserved via --railin; bars
    # keep full-width chrome.
    block = re.search(r"\.block\{([^}]*)\}", PAGE).group(1)
    assert "margin:0 auto 14px" in block and "max-width:960px" in block
    hwrap = re.search(r"#hwrap\{([^}]*)\}", PAGE).group(1)
    assert "max-width:960px" in hwrap and "margin:0 auto" in hwrap
    chatinner = re.search(r"\.chatinner\{([^}]*)\}", PAGE).group(1)
    assert "max-width:960px" in chatinner and "margin:0 auto" in chatinner
    assert 'class="chatinner"' in PAGE
    turnend = re.search(r"\.turnend\{([^}]*)\}", PAGE).group(1)
    assert "margin:20px auto" in turnend and "max-width:960px" in turnend
    # --railin compensation: header/composer right padding = pad + gutter.
    pane = re.search(r"#pane\{([^}]*)\}", PAGE).group(1)
    assert "--railw:18px" in pane and "--railin:0px" in pane
    head = re.search(r"#sesshead\{([^}]*)\}", PAGE).group(1)
    assert "calc(26px + var(--railin))" in head
    assert "border-bottom:1px solid var(--border)" in head  # full-width chrome kept
    bar = re.search(r"#chatbar\{([^}]*)\}", PAGE).group(1)
    assert "calc(26px + var(--railin))" in bar
    assert "border-top:1px solid var(--border)" in bar
    # The redundant latest button is gone entirely — markup, CSS, JS, i18n.
    assert "backtop" not in PAGE and "nav.latest" not in PAGE


def test_composer_status_never_skews_input_row():
    # The send-status line lives on its own row under the input, on the same
    # 960px axis — it can never push the visible textarea/send group off
    # center, whether empty or populated.
    chatbar = re.search(
        r'<div id="chatbar">([\s\S]*?)</div>\s*</div>\s*</div>', PAGE).group(1)
    inner = re.search(
        r'<div class="chatinner">([\s\S]*?)</div>', chatbar).group(1)
    assert 'id="chatstatus"' not in inner, "status must not sit in the input row"
    assert 'id="chatstatus"' in chatbar and 'aria-live="polite"' in chatbar
    status_css = re.search(r"#chatstatus\{([^}]*)\}", PAGE).group(1)
    assert "max-width:960px" in status_css and "margin:8px auto 0" in status_css
    # No reserved inline width ever — the old min-width:96px flex skew is
    # gone, and an empty status collapses to zero height.
    assert "min-width" not in status_css
    assert re.search(r"#chatstatus:empty\{[^}]*display:none", PAGE)


# ---------- i18n: bundled en / zh-CN / zh-TW dictionaries ----------


def _locales():
    """Parse the embedded LOCALES dictionary. It is written as a
    JSON-compatible literal (``const LOCALES=Object.freeze({...});``) so the
    completeness contract can be checked without executing the page."""
    m = re.search(r"const LOCALES=Object\.freeze\((\{[\s\S]*?\n\})\);", PAGE)
    assert m, "LOCALES frozen dictionary missing"
    return json.loads(m.group(1))


def test_i18n_dictionaries_complete_and_identical():
    locales = _locales()
    assert set(locales) == {"en", "zh-CN", "zh-TW"}
    keysets = {loc: set(d) for loc, d in locales.items()}
    assert keysets["en"], "English dictionary must not be empty"
    assert keysets["zh-CN"] == keysets["en"] == keysets["zh-TW"]
    for d in locales.values():
        for k, v in d.items():
            assert isinstance(k, str) and k
            if isinstance(v, dict):  # plural map
                assert v and set(v) <= {"zero", "one", "two", "few", "many", "other"}
                assert all(isinstance(x, str) and x for x in v.values())
            else:
                assert isinstance(v, str) and v


def test_i18n_glossary_exactness():
    loc = _locales()
    en, zh, tw = loc["en"], loc["zh-CN"], loc["zh-TW"]
    # Deliberate product translations — not machine-translation output.
    assert zh["status.proc.running"] == "运行中" and tw["status.proc.running"] == "執行中"
    assert zh["status.proc.ready"] == "就绪" and tw["status.proc.ready"] == "就緒"
    assert zh["status.proc.idle"] == "空闲" and tw["status.proc.idle"] == "閒置"
    assert zh["status.proc.done"] == "已完成" and tw["status.proc.done"] == "已完成"
    assert zh["status.proc.failed"] == "失败" and tw["status.proc.failed"] == "失敗"
    assert zh["transcript.dispatched_message"] == "已派发消息"
    assert tw["transcript.dispatched_message"] == "已派發訊息"
    assert zh["transcript.user_message"] == "用户消息"
    assert tw["transcript.user_message"] == "使用者訊息"
    assert zh["transcript.thinking"] == "思考中" and tw["transcript.thinking"] == "思考中"
    assert zh["transcript.turn_ended"] == "回合已结束" and tw["transcript.turn_ended"] == "回合已結束"
    assert zh["rail.turn_end"] == "回合结束" and tw["rail.turn_end"] == "回合結束"
    assert zh["send.waiting_busy"] == "等待 Agent——会话正忙…"
    assert tw["send.waiting_busy"] == "等待 Agent——工作階段忙碌中…"
    assert zh["send.dispatched"] == "已派发" and tw["send.dispatched"] == "已派發"
    assert zh["send.failed"] == "发送失败" and tw["send.failed"] == "傳送失敗"
    assert zh["status.live"] == "实时" and tw["status.live"] == "即時"
    assert zh["status.disconnected"] == "已断开" and tw["status.disconnected"] == "已中斷"
    assert zh["empty.loading"] == "加载中…" and tw["empty.loading"] == "載入中…"
    # "token" stays a technical term in every locale — never 令牌.
    for d in (en, zh, tw):
        assert "token" in d["tokens.run"].lower()
        assert "令牌" not in json.dumps(d, ensure_ascii=False)
    # Simplified and Traditional are separate dictionaries, not a conversion.
    assert zh["transcript.user_message"] != tw["transcript.user_message"]
    assert zh["a11y.sessions"] != tw["a11y.sessions"]
    assert zh["status.proc.running"] != tw["status.proc.running"]


def test_i18n_static_hooks_resolve():
    locales = _locales()
    hooks = set(re.findall(r'data-i18n(?:-[\w-]+)?="([^"]+)"', PAGE))
    assert hooks, "no data-i18n hooks found"
    for key in hooks:
        for loc in ("en", "zh-CN", "zh-TW"):
            assert key in locales[loc], f"{key} missing in {loc}"
    for needle in (
        'data-i18n="nav.subagents"',
        'data-i18n="session.select"',
        'data-i18n="theme.label"',
        'data-i18n="language.label"',
        'data-i18n-aria-label="a11y.sessions"',
        'data-i18n-aria-label="a11y.show_session_list"',
        'data-i18n-aria-label="a11y.message_selected_session"',
        'data-i18n-aria-label="a11y.send_message"',
        'data-i18n-aria-label="rail.label"',
        'data-i18n-placeholder="chat.placeholder.default"',
        'data-i18n-title="theme.follow_system"',
        'data-i18n-title="chat.send"',
    ):
        assert needle in PAGE


def test_i18n_locale_selection_and_persistence():
    for needle in (
        '"ab-locale"',
        "LOCALE_KEY",
        "localePref",
        "resolveSystemLocale",
        "setLocalePref",
        "applyLocale",
        "rerenderLocale",
        "languagechange",
        '"storage"',
        'id="langsel"',
        'for="langsel"',
        'value="system"',
        'value="en"',
        'value="zh-CN"',
        'value="zh-TW"',
        "简体中文",
        "繁體中文",
        "English",
        "documentElement.lang",
        "document.title",
        "localStorage.getItem(LOCALE_KEY)",
        "localStorage.setItem(LOCALE_KEY",
    ):
        assert needle in PAGE
    # <html lang> is resolved pre-render by the head bootstrap.
    head = PAGE.split("<style>")[0]
    assert '"ab-locale"' in head and "navigator.languages" in head
    assert "document.documentElement.lang" in head
    # zh-Hant/TW/HK/MO -> zh-TW; zh/Hans/CN/SG/MY -> zh-CN; en-* -> en.
    assert "hant|tw|hk|mo" in PAGE
    assert 'LOCALE_PREFS=["system","en","zh-CN","zh-TW"]' in PAGE
    # languagechange only matters while the preference stays "system".
    assert 'langPref==="system"' in PAGE


def test_i18n_locale_switch_rerenders_from_cache():
    body = re.search(r"function rerenderLocale\(\)\{([\s\S]*?)\n\}", PAGE).group(1)
    # Stashed panes are locale-bound — dropped, then the pane is rebuilt from
    # eventsCache (never a transcript refetch).
    assert "dropPane" in body and "startReplay" in body and "eventsCache" in body
    assert "lastSidebarSig" in body  # sidebar labels are baked into its DOM
    assert "refreshComposer" in body  # send status re-renders from key+params
    assert "scheduleRail" in body
    assert "pollEvents" not in body and "fetch(" not in body


def test_i18n_no_machine_translation_or_network():
    lower = PAGE.lower()
    for gone in ("googleapis", "translate_a", "client=gtx", "google translate", "gtx"):
        assert gone not in lower
    # translateY() in the rail is a CSS transform — all real fetches are
    # same-origin relative URLs, so localization never leaves localhost.
    assert not re.search(r'fetch\(\s*[`\'"]https?', PAGE)
    # Localized strings stay plain text; untrusted/template interpolations in
    # innerHTML keep passing through esc().
    assert PAGE.count("esc(t(") >= 10


def test_i18n_send_state_codes():
    # The send status line stores {key, params, detail, final} — never
    # rendered text — so a locale switch re-renders without network calls.
    assert re.search(r"const sendState=\{\};.*\{name, key, params, detail, final\}", PAGE)
    for needle in (
        '"send.sending"',
        '"send.queued"',
        '"send.waiting_busy"',
        '"send.waiting_owner"',
        '"send.delivering"',
        '"send.dispatched_task"',
        '"send.failed"',
        "SEND_ERR",
        "sendErrState",
        "sendKey(j)",
        "renderSendStatus",
        "refreshComposer",
    ):
        assert needle in PAGE
    # error_code drives the localized label; the raw error survives only as
    # tooltip detail.
    assert "j.error_code" in PAGE and "st.detail" in PAGE
    # No rendered-English storage from the old contract.
    assert "sendState[sid].text" not in PAGE and "setChatStatus(" not in PAGE


def test_i18n_send_err_covers_emitted_codes():
    """Every error_code the bridge or the dashboard's send endpoints emit maps
    to a localized label — a raw English diagnostic can only ever surface in
    the tooltip, never as the status line."""
    send_err = dict(
        re.findall(
            r'(\w+):"(send\.[\w.]+)"',
            re.search(r"const SEND_ERR=\{([\s\S]*?)\};", PAGE).group(1),
        )
    )
    agent_bridge_dir = Path(dashboard.__file__).parent.parent
    emitted = set(
        re.findall(
            r'"error_code":\s*"(\w+)"',
            (agent_bridge_dir / "registry.py").read_text(encoding="utf-8")
            + Path(dashboard.__file__).read_text(encoding="utf-8"),
        )
    )
    # not_found is endpoint routing only — unreachable from the page's fixed
    # /api/* URLs — and keeps the generic localized send.failed fallback.
    emitted.discard("not_found")
    missing = emitted - set(send_err)
    assert not missing, f"error codes without a localized label: {missing}"
    locales = _locales()
    for key in send_err.values():
        for loc in ("en", "zh-CN", "zh-TW"):
            assert key in locales[loc], f"{key} missing in {loc}"


def test_i18n_invalid_timestamp_guards():
    """ago()/fmtTs() render nothing for missing/unparseable input — no
    English 'Invalid Date' or 'NaN 天前' can leak into any locale."""
    fmt = re.search(r"const fmtTs=([\s\S]*?)\nconst ago=", PAGE).group(1)
    assert "v==null" in fmt and "Number.isFinite(+d)" in fmt
    body = re.search(r"const ago=([\s\S]*?)\nfunction applyStatic", PAGE).group(1)
    assert "v==null" in body and "Number.isFinite(s)" in body
    # addToolStatus's inline duration math is the other NaN path: guard it.
    tool = re.search(r"function addToolStatus\(e\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "Number.isFinite(s)" in tool
    # Per-locale executable coverage lives in dashboard_status_behavior.js.


def test_i18n_cjk_thinking_count():
    """Word counts mislead for space-less scripts — predominantly-CJK thinking
    text counts characters; space-separated text keeps word counts."""
    locales = _locales()
    for loc in ("en", "zh-CN", "zh-TW"):
        d = locales[loc]
        assert "transcript.thinking_words" in d and "transcript.thinking_chars" in d
    assert "CJK_RE" in PAGE and "function thinkLabel(" in PAGE
    flush = re.search(r"function flushBlocks\(\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "thinkLabel(b._text)" in flush


def test_i18n_zh_tw_context_terminology():
    # 上下文 is the established zh-CN/zh-TW term for the context window — the
    # occupancy tooltip and the breakdown label must agree on it.
    locales = _locales()
    for loc in ("zh-CN", "zh-TW"):
        d = locales[loc]
        assert "上下文" in d["tokens.context"]
        assert "上下文" in d["tokens.ctx_only"]


def test_i18n_no_t_param_shadowing():
    """No parameter or callback arg may shadow the global translate function
    t() — a `t` param would silently break a future t() call in that body."""
    for sig in (
        "function taskDur(tk)",
        "function durSpan(tk,cls)",
        "function tokSpan(tk,cls,live)",
        "forEach((tk,i)=>",
    ):
        assert sig in PAGE
    for gone in (
        "function taskDur(t)",
        "function durSpan(t,cls)",
        "function tokSpan(t,cls,live)",
        "forEach((t,i",
    ):
        assert gone not in PAGE


def test_i18n_first_paint_fouc_guard():
    """Non-English locales hide English fallback text until applyStatic()
    localizes it, with a timed failsafe so a dead body script can never leave
    the chrome invisible."""
    head = PAGE.split("<style>")[0]
    assert 'setAttribute("data-i18n-pending"' in head
    assert 'removeAttribute("data-i18n-pending")' in head and "setTimeout" in head
    assert '_lang!=="en"' in head  # English fallback is already final
    # The pre-render title map mirrors LOCALES app.title for every locale.
    for d in _locales().values():
        assert f'"{d["app.title"]}"' in head
    assert re.search(r"html\[data-i18n-pending\] \[data-i18n\]\{visibility:hidden\}", PAGE)
    apply = re.search(r"function applyStatic\(\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert 'removeAttribute("data-i18n-pending")' in apply


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


def test_index_security_headers(dash):
    """The dashboard page ships a restrictive CSP: the single-file design
    needs inline script/style, the avatar assets are data: images, and the
    API surface is same-origin — everything else is hard-blocked."""
    _, base = dash
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        csp = r.headers.get("Content-Security-Policy")
        nosniff = r.headers.get("X-Content-Type-Options")
    assert csp is not None
    for directive in (
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in csp
    assert nosniff == "nosniff"


def test_page_has_no_external_subresources():
    """Belt and suspenders under the CSP: the page must not reference any
    external origin — no remote script/style/image/font loads and no
    non-relative fetch/beacon/socket targets."""
    for pat in (
        r'src=["\']https?://',
        r'href=["\']https?://',
        r"url\(\s*[\"']?https?://",
        r"@import",
        r'fetch\(\s*[`\'"]https?',
        r'sendBeacon\(\s*[`\'"]https?',
        r"new\s+WebSocket",
        r"new\s+EventSource",
        r"XMLHttpRequest",
    ):
        assert not re.search(pat, PAGE), pat


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
    }
    # task timing fields round-trip so the page can compute working durations
    assert j["tasks"][0]["started_at"] == "2026-09-12T10:00:02Z"
    assert j["tasks"][0]["finished_at"] == "2026-09-12T10:01:42Z"
    # Batched live-usage map rides the same response; nothing runs in the
    # fixture so it stays empty.
    assert j["live"] == {}


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
    code, body = _get(base + "/api/events?session=../evil&offset=0")
    assert code == 400
    j = json.loads(body)
    assert j["error"] == "bad session" and j["error_code"] == "bad_session"


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
    assert j["error_code"] == "missing"
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
    assert code == 404 and json.loads(body)["error_code"] == "unknown_session"
    code, body = _post(base + "/api/send", {"session": "sess_1", "text": "  "})
    assert code == 400 and json.loads(body)["error_code"] == "empty_or_too_long"
    code, body = _post(base + "/api/send", {"session": "bad id!", "text": "x"})
    assert code == 400 and json.loads(body)["error_code"] == "bad_session"


# ---------- batched live usage map (/api/overview "live") ----------

LIVE_SID = "sess_live"


def _usage_rec(ts, total):
    return {
        "type": "usage",
        "ts": ts,
        "data": {
            "update_type": "UsageUpdate",
            "consumed": {"scope": "run", "quality": "exact", "total": total},
        },
    }


def _chunk_rec(ts, text="x"):
    return {"type": "message_chunk", "ts": ts, "data": {"text": text}}


def _write_transcript(home, sid, records):
    path = home / "transcripts" / f"{sid}.jsonl"
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _set_state(home, tasks, sessions=None):
    state = {"sessions": sessions or [SESSION], "tasks": tasks}
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _live_task(sid=LIVE_SID, tid="task_live", started="2026-09-12T10:00:10Z"):
    return {
        "task_id": tid,
        "session_id": sid,
        "agent": "devin",
        "status": "running",
        "created_at": "2026-09-12T10:00:00Z",
        "started_at": started,
    }


def _overview(base):
    code, body = _get(base + "/api/overview")
    assert code == 200
    return json.loads(body)


def test_overview_live_map_last_qualifying_usage(dash):
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(
        home,
        LIVE_SID,
        [
            _usage_rec("2026-09-12T10:00:05Z", 99),  # pre-start: prior run
            _usage_rec("2026-09-12T10:00:20Z", 10),
            _usage_rec("2026-09-12T10:00:30Z", 25),
        ],
    )
    _set_state(home, [_live_task()])
    j = _overview(base)
    entry = j["live"][LIVE_SID]
    # Pinned to the exact task; only the last post-start snapshot counts.
    assert entry["task_id"] == "task_live"
    assert entry["consumed"]["total"] == 25


def test_overview_live_covers_sibling_sessions(dash):
    """Sibling-owned tasks share state.json + transcripts/, so the batch
    covers them uniformly with no owner check."""
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(home, "sess_a", [_usage_rec("2026-09-12T10:00:20Z", 5)])
    _write_transcript(home, "sess_b", [_usage_rec("2026-09-12T10:00:20Z", 7)])
    _set_state(
        home,
        [_live_task("sess_a", "task_a"), _live_task("sess_b", "task_b")],
    )
    j = _overview(base)
    assert j["live"]["sess_a"]["consumed"]["total"] == 5
    assert j["live"]["sess_b"]["consumed"]["total"] == 7
    assert j["live"]["sess_b"]["task_id"] == "task_b"


def test_live_incremental_reads_appends_only(dash, monkeypatch):
    """Steady-state polls are one stat + the appended bytes: the transcript
    is opened on the first sighting and after each append, never reread."""
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    path = _write_transcript(home, LIVE_SID, [_usage_rec("2026-09-12T10:00:20Z", 10)])
    _set_state(home, [_live_task()])
    opens = []
    real_open = open

    def spy(file, *args, **kwargs):
        if str(file).endswith(".jsonl"):
            opens.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(dashboard, "open", spy, raising=False)
    j = _overview(base)
    assert j["live"][LIVE_SID]["consumed"]["total"] == 10
    assert len(opens) == 1  # first-touch seed scan
    _overview(base)
    _overview(base)
    assert len(opens) == 1  # stable polls never reopen the transcript
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_usage_rec("2026-09-12T10:00:40Z", 41)) + "\n")
    j = _overview(base)
    assert j["live"][LIVE_SID]["consumed"]["total"] == 41
    assert len(opens) == 2  # appended bytes only
    # An incomplete trailing line waits for its newline; the offset stays put.
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type":"usage","ts":"2026-09-12T10:00:50Z","data":{"consumed":{"total":55}}')
    j = _overview(base)
    assert j["live"][LIVE_SID]["consumed"]["total"] == 41
    with open(path, "a", encoding="utf-8") as f:
        f.write("}\n")
    j = _overview(base)
    assert j["live"][LIVE_SID]["consumed"]["total"] == 55


def test_live_rotation_resets_reader(dash):
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(
        home,
        LIVE_SID,
        [_usage_rec("2026-09-12T10:00:20Z", 10), _usage_rec("2026-09-12T10:00:30Z", 25)],
    )
    _set_state(home, [_live_task()])
    assert _overview(base)["live"][LIVE_SID]["consumed"]["total"] == 25
    # Rotate/truncate: a smaller fresh file must reset the cached offset.
    _write_transcript(home, LIVE_SID, [_usage_rec("2026-09-12T10:00:35Z", 7)])
    assert _overview(base)["live"][LIVE_SID]["consumed"]["total"] == 7


def test_live_new_started_at_resets(dash):
    """A new run on the same session must not inherit the prior run's
    usage events — the entry resets on the started_at change."""
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(home, LIVE_SID, [_usage_rec("2026-09-12T10:00:20Z", 10)])
    _set_state(home, [_live_task()])
    assert _overview(base)["live"][LIVE_SID]["consumed"]["total"] == 10
    _set_state(home, [_live_task(started="2026-09-12T10:00:50Z")])
    assert LIVE_SID not in _overview(base)["live"]


def test_live_bad_session_id_skipped(dash):
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _set_state(home, [_live_task("../evil", "task_evil")])
    j = _overview(base)
    assert "../evil" not in j["live"]
    assert "../evil" not in dashboard._LIVE_TAIL


def test_live_prunes_finished_sessions(dash):
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(home, LIVE_SID, [_usage_rec("2026-09-12T10:00:20Z", 10)])
    _set_state(home, [_live_task()])
    _overview(base)
    assert LIVE_SID in dashboard._LIVE_TAIL
    _set_state(
        home,
        [
            {
                **_live_task(),
                "status": "completed",
                "finished_at": "2026-09-12T10:01:00Z",
            }
        ],
    )
    j = _overview(base)
    assert LIVE_SID not in j["live"]
    assert LIVE_SID not in dashboard._LIVE_TAIL


def test_live_seed_fallback_full_scan(dash):
    """A qualifying usage event older than the seed window is still found —
    the reader falls back to one bounded full scan on first touch."""
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(
        home,
        LIVE_SID,
        [
            _usage_rec("2026-09-12T10:00:20Z", 12),
            _chunk_rec("2026-09-12T10:00:30Z", "y" * (dashboard.LIVE_SEED_BYTES + 4096)),
        ],
    )
    _set_state(home, [_live_task()])
    j = _overview(base)
    assert j["live"][LIVE_SID]["consumed"]["total"] == 12
    assert dashboard._LIVE_TAIL[LIVE_SID]["offset"] > 0


def test_live_no_usage_yet(dash):
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(home, LIVE_SID, [_chunk_rec("2026-09-12T10:00:20Z", "hi")])
    _set_state(home, [_live_task()])
    assert _overview(base)["live"] == {}


def test_live_timestamps_compare_as_instants(dash):
    """Mixed-offset stamps compare by absolute instant, not text: a Z event
    that sorts textually before a +08:00 started_at can still post-date it.
    """
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(
        home,
        LIVE_SID,
        [
            _usage_rec("2026-09-12T01:59:00Z", 99),  # before the start instant
            _usage_rec("2026-09-12T03:00:00Z", 42),  # after it, lexically "earlier"
        ],
    )
    # 10:00:05+08:00 == 02:00:05Z — a lexicographic compare would drop 03:00Z.
    _set_state(home, [_live_task(started="2026-09-12T10:00:05+08:00")])
    j = _overview(base)
    assert j["live"][LIVE_SID]["consumed"]["total"] == 42
    # Cross-day positive offset: 00:30+08:00 == 16:30Z of the previous day;
    # the 17:00Z event post-dates it though its date string is "earlier".
    dashboard._LIVE_TAIL.clear()
    _write_transcript(home, "sess_day", [_usage_rec("2026-09-12T17:00:00Z", 8)])
    _set_state(
        home,
        [
            _live_task(),
            _live_task("sess_day", "task_day", started="2026-09-13T00:30:00+08:00"),
        ],
    )
    j = _overview(base)
    assert j["live"]["sess_day"]["consumed"]["total"] == 8


def test_live_timestamps_equivalent_offsets(dash):
    """The same instant written with Z and an offset qualifies identically —
    the boundary itself is inclusive."""
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(
        home,
        LIVE_SID,
        [_usage_rec("2026-09-12T18:00:05+08:00", 7)],  # == started_at instant
    )
    _set_state(home, [_live_task(started="2026-09-12T10:00:05Z")])
    assert _overview(base)["live"][LIVE_SID]["consumed"]["total"] == 7


def test_live_timestamps_invalid_fail_closed(dash):
    """Unparseable event stamps are ignored; an unparseable started_at
    yields no live entry at all."""
    home, base = dash
    dashboard._LIVE_TAIL.clear()
    _write_transcript(
        home,
        LIVE_SID,
        [_usage_rec("not-a-time", 5), _usage_rec("2026-09-12T10:00:20Z", 10)],
    )
    _set_state(home, [_live_task()])
    j = _overview(base)
    assert j["live"][LIVE_SID]["consumed"]["total"] == 10
    _set_state(home, [_live_task(started="garbage")])
    assert LIVE_SID not in _overview(base)["live"]


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


def test_presence_survives_throttled_heartbeat(monkeypatch):
    """A hidden tab's ~60s intensive-throttle cadence must never expire the
    lease between beats; a genuinely gone client still expires past the TTL."""
    assert dashboard.PRESENCE_TTL >= 120.0  # spans a fully missed 60s wake-up
    clock = [1000.0]
    monkeypatch.setattr(dashboard.time, "monotonic", lambda: clock[0])
    dashboard.PRESENCE.clear()
    try:
        dashboard.presence_update("tab", False)
        clock[0] += 60
        assert dashboard.presence_count() == 1  # throttled beat still alive
        clock[0] += 60
        assert dashboard.presence_count() == 1  # one missed wake-up tolerated
        clock[0] += dashboard.PRESENCE_TTL
        assert dashboard.presence_count() == 0  # stale presence expires
        dashboard.presence_update("tab", False)
        dashboard.presence_update("tab", True)
        assert dashboard.presence_count() == 0  # pagehide bye removes at once
    finally:
        dashboard.PRESENCE.clear()


def test_second_dashboard_bind_fails(dash):
    """A second server on the same port must fail — Windows SO_REUSEADDR once
    let a duplicate dashboard coexist with a split PRESENCE table."""
    _, base = dash
    port = int(base.rsplit(":", 1)[1])
    with pytest.raises(OSError):
        dashboard.DashboardServer(("127.0.0.1", port), dashboard.Handler)
    # Even a plain SO_REUSEADDR server must not steal the exclusive bind.
    with pytest.raises(OSError):
        ThreadingHTTPServer(("127.0.0.1", port), dashboard.Handler)


def test_presence_heartbeat_lifecycle():
    """The heartbeat block keeps the presence contract: fresh per-page id,
    immediate re-registration on every lifecycle recovery event."""
    beat = re.search(r"presence heartbeat.*?\*/([\s\S]*?)const esc=", PAGE)
    assert beat, "presence heartbeat block missing"
    js = beat.group(1)
    # Per-page id: no sessionStorage carry-over — a duplicated tab gets its
    # own lease and its bye cannot remove the original's presence.
    assert "sessionStorage" not in js
    assert "crypto.randomUUID" in js
    for ev in ('"pageshow"', '"focus"', '"online"', '"visibilitychange"', '"pagehide"'):
        assert f"addEventListener({ev}" in js
    assert "sendBeacon" in js
    # The lease must comfortably outlive the throttled cadence: TTL >= 2x the
    # ~60s Chrome intensive-throttle bucket, beat interval far under the TTL.
    assert dashboard.PRESENCE_TTL >= 120.0
    ms = int(re.search(r"setInterval\(\(\)=>ping\(0\),(\d+)\)", js).group(1))
    assert ms * 10 <= dashboard.PRESENCE_TTL * 1000
