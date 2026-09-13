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
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BRIDGE_DIR = Path(os.environ.get("BRIDGE_DIR", Path(__file__).resolve().parent))
STATE_FILE = BRIDGE_DIR / "state.json"
TRANSCRIPT_DIR = BRIDGE_DIR / "transcripts"
OUTBOX_DIR = BRIDGE_DIR / "outbox"

# Open-tab presence: browser heartbeats via /api/presence; the bridge checks
# /api/client_state to decide whether to pop the dashboard up.
PRESENCE: dict[str, float] = {}  # client_id -> last-seen monotonic timestamp
PRESENCE_LOCK = threading.Lock()
PRESENCE_TTL = 45.0


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


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

MAX_INPUT_CHARS = 4000


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return {"sessions": [], "tasks": []}


def normalize_event(rec):
    """Convert a raw transcript JSONL record into a compact dashboard event."""
    t = rec.get("type")
    ts = rec.get("ts")
    d = rec.get("data") or {}

    if t == "prompt_sent":
        return {
            "t": "prompt",
            "ts": ts,
            "text": d.get("text", ""),
            "src": d.get("source"),
            "task": d.get("task_id"),
        }
    if t == "message_chunk":
        return {"t": "msg", "ts": ts, "text": d.get("text", "")}
    if t == "thought_chunk":
        return {"t": "think", "ts": ts, "text": d.get("text", "")}
    if t == "tool_call":
        raw_input = d.get("input")
        inp = raw_input
        if isinstance(raw_input, str):
            try:
                inp = json.loads(raw_input)
            except Exception:
                inp = raw_input
        s = json.dumps(inp, ensure_ascii=False) if not isinstance(inp, str) else inp
        if s and len(s) > MAX_INPUT_CHARS:
            s = s[:MAX_INPUT_CHARS] + "…"
        return {
            "t": "tool",
            "ts": ts,
            "id": d.get("tool_call_id"),
            "kind": d.get("kind") or "tool",
            "title": d.get("title") or "",
            "input": s,
        }
    if t == "tool_call_update":
        status = d.get("status")
        if not status:
            return None
        return {"t": "tool_status", "ts": ts, "id": d.get("tool_call_id"), "status": status}
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


def read_events(session_id, offset):
    """Read normalized events from a transcript starting at byte offset."""
    if not SAFE_ID.match(session_id or ""):
        return None, 0
    path = TRANSCRIPT_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if offset > size:  # file shrank / rotated — restart
        offset = 0
    with open(path, "rb") as f:
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


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Bridge Dashboard</title>
<script>try{var p=localStorage.getItem("ab-theme");
document.documentElement.setAttribute("data-theme",
  p==="light"||p==="dark"?p:(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"))}catch(e){}</script>
<style>
:root{
  color-scheme:light;
  --font-sans:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;
  --font-mono:"Source Code Pro",Consolas,Menlo,monospace;
  --text:#1a1a1a; --dim:#666666; --dimmer:#909090;
  --panel:#ffffff; --panel2:#f8f9fa; --panel3:#f1f3f4;
  --border:#dedede; --accent:#3366cc; --accent-hover:#2a5594; --accent-tint:#eef3fc;
  --green:#2e7d32; --green-bg:#e6f7e9; --amber:#ed6c02; --amber-bg:#fff4e5;
  --red:#c62828; --red-bg:#ffebee;
  --on-accent:#ffffff; --shadow:0 2px 8px rgba(0,0,0,.1);
}
[data-theme="dark"]{
  color-scheme:dark;
  --text:#e4e6e8; --dim:#9ba1a7; --dimmer:#6d7278;
  --panel:#0f1113; --panel2:#16181b; --panel3:#1e2125;
  --border:#292d32; --accent:#8b949e; --accent-hover:#adbac4; --accent-tint:#23262b;
  --green:#4ac26b; --green-bg:#15241a; --amber:#d29922; --amber-bg:#282113;
  --red:#f85149; --red-bg:#2c1517;
  --on-accent:#0f1113; --shadow:0 2px 8px rgba(0,0,0,.5);
}
*{box-sizing:border-box;
  scrollbar-width:thin;
  scrollbar-color:color-mix(in srgb,var(--dimmer) 55%,transparent) transparent}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
::-webkit-scrollbar-thumb{border-radius:6px;border:2px solid transparent;
  background:color-mix(in srgb,var(--dimmer) 55%,transparent);background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--dimmer);background-clip:padding-box}
html,body{height:100%}
body{margin:0;font:14px/1.5 var(--font-sans);background:var(--panel);color:var(--text);
  height:100vh;display:flex;overflow:hidden}
.app{width:100%;min-height:0;display:flex;overflow:hidden;background:var(--panel)}
.ic{flex:none;vertical-align:-2px}
/* ---------- sidebar ---------- */
#sidebar{width:300px;min-width:250px;flex:none;background:var(--panel2);
  border-right:1px solid var(--border);display:flex;flex-direction:column}
.side-head{display:flex;align-items:center;justify-content:space-between;
  padding:18px 18px 8px}
.side-head h2{font-size:16px;font-weight:700;margin:0;letter-spacing:-.01em}
#live{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--dim)}
#live .ldot{width:7px;height:7px;border-radius:50%;background:var(--dimmer);flex:none}
#live.on{color:var(--green)}
#live.on .ldot{background:var(--green);animation:pulse 1.6s ease-in-out infinite}
#live.off{color:var(--red)}
#live.off .ldot{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
#sesslist{flex:1;overflow-y:auto;padding:4px 10px 14px}
.side-foot{flex:none;border-top:1px solid var(--border);display:flex;align-items:center;
  gap:8px;padding:4px 12px}
.side-foot-label{flex:none;font-size:11px;font-weight:600;color:var(--dimmer);
  letter-spacing:.04em;margin:0}
.theme-options{flex:1;display:flex;gap:4px}
.theme-options [role="radio"]{flex:1;display:flex;flex-direction:row;align-items:center;
  justify-content:center;gap:3px;padding:3px 4px;font:inherit;font-size:11px;color:var(--dim);
  background:transparent;border:1px solid var(--border);border-radius:7px;cursor:pointer}
.theme-options [role="radio"]:hover{background:var(--panel3)}
.theme-options [role="radio"][aria-checked="true"]{background:var(--panel3);
  border-color:var(--dim);color:var(--text)}
.theme-options [role="radio"]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.sess{display:flex;gap:10px;align-items:flex-start;width:100%;text-align:left;
  font:inherit;color:var(--text);background:none;border:none;border-radius:8px;
  padding:9px 10px;cursor:pointer;position:relative}
.sess:hover{background:color-mix(in srgb,var(--text) 5%,transparent)}
.sess:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.sess.sel{background:var(--accent-tint)}
.sess.sel::before{content:"";position:absolute;left:0;top:9px;bottom:9px;width:3px;
  border-radius:2px;background:var(--accent)}
.sicon{flex:none;width:20px;height:20px;display:flex;align-items:center;
  justify-content:center;color:var(--dim);margin-top:1px}
.smeta{flex:1;min-width:0}
.stitle{display:block;font-weight:600;font-size:13px;line-height:1.35;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sstatus{display:flex;align-items:center;gap:6px;font-size:12px;margin-top:3px;
  color:var(--dim)}
.sgr{display:inline-flex;align-items:center;flex:none}
.ssub{display:block;font-size:11.5px;color:var(--dimmer);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.glyph--running{color:var(--accent)}
.glyph--warning{color:var(--amber)}
.glyph--success{color:var(--green)}
.glyph--error{color:var(--red)}
.glyph--neutral{color:var(--dimmer)}
.spin{display:inline-flex;flex:none;animation:ui-spin .8s linear infinite}
@keyframes ui-spin{to{transform:rotate(360deg)}}
.subav-wrap{position:relative;display:inline-flex;flex:none;line-height:0}
.subav{display:block;border-radius:6px}
.subav-d{display:none}
[data-theme="dark"] .subav-l{display:none}
[data-theme="dark"] .subav-d{display:block}
/* Busy sessions pulse the subagent avatar itself (Codex working-dot timing):
   scale only, 1.25s ease-in-out, transform-origin center. */
.subav-wrap.pulse{animation:ui-pulse 1.25s ease-in-out infinite;
  transform-origin:50%;will-change:transform;backface-visibility:hidden}
@keyframes ui-pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.25)}}
.sdur,.hdur,.stok,.htok{color:var(--dimmer);font-variant-numeric:tabular-nums;
  white-space:nowrap}
/* ---------- pane header ---------- */
#pane{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--panel)}
#sesshead{display:flex;align-items:center;gap:14px;padding:16px 26px 14px;
  border-bottom:1px solid var(--border);min-height:78px}
#menubtn{display:none;flex:none;width:34px;height:34px;align-items:center;
  justify-content:center;background:none;border:1px solid var(--border);
  border-radius:8px;color:var(--dim);cursor:pointer}
#menubtn:hover{background:var(--panel3)}
#hwrap{flex:1;display:flex;align-items:center;gap:14px;min-width:0}
.avatar{flex:none;width:46px;height:46px;display:flex;align-items:center;
  justify-content:center}
.hbody{flex:1;min-width:0}
.htitle{font-size:17px;font-weight:700;margin:0;line-height:1.3;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hsub{display:flex;align-items:center;gap:9px;margin-top:4px;flex-wrap:wrap;
  font-size:12px;color:var(--dim)}
.hstatus{display:inline-flex;align-items:center;gap:6px;font-weight:600;color:var(--text)}
.badge{display:inline-block;padding:1px 8px;border-radius:6px;font-size:11px;
  font-weight:600;background:var(--panel3);border:1px solid var(--border);color:var(--dim)}
.hsep{color:var(--dimmer)}
.hrepo{display:inline-flex;align-items:center;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;min-width:0;max-width:34ch}
.hmeta{flex:none;margin-left:auto;font-size:11.5px;color:var(--dimmer);white-space:nowrap}
.hplaceholder{color:var(--dimmer);font-size:14px}
/* ---------- conversation cards ---------- */
#content{flex:1;overflow-y:auto;padding:20px 26px 24px}
.block{margin:0 0 14px;max-width:960px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  padding:13px 16px}
.chead{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;
  color:var(--dim);margin-bottom:8px}
.chead .ctime{margin-left:auto;font-weight:400;color:var(--dimmer);font-size:11px}
.card.prompt .chead{color:var(--accent)}
.card.prompt.user{background:var(--accent-tint);border-left:3px solid var(--accent)}
.card pre.ptext{white-space:pre-wrap;word-break:break-word;margin:0;
  font:inherit;font-size:14px}
.msg-body{font-size:14px}
.msg-body pre{background:var(--panel2);border:1px solid var(--border);border-radius:8px;
  padding:10px;overflow-x:auto;font:12.5px/1.5 var(--font-mono)}
.msg-body code{font-family:var(--font-mono);background:var(--panel3);border-radius:4px;
  padding:1px 5px;font-size:12.5px}
.msg-body pre code{background:none;padding:0}
.msg-body h1,.msg-body h2,.msg-body h3,.msg-body h4{margin:12px 0 6px;font-size:15px}
.msg-body ul,.msg-body ol{margin:6px 0;padding-left:22px}
.msg-body p{margin:6px 0}
details.think{background:var(--panel);border:1px solid var(--border);border-radius:12px}
details.think summary{list-style:none;display:flex;align-items:center;gap:8px;
  padding:11px 14px;cursor:pointer;font-size:12px;font-weight:600;color:var(--dim)}
details.think summary::-webkit-details-marker{display:none}
details.think summary .ic{color:var(--accent)}
details.think summary .chev{margin-left:auto;display:inline-flex;color:var(--dimmer)}
details.think summary .chev .ic{color:var(--dimmer)}
details.think[open] summary{border-bottom:1px solid var(--panel3)}
details.think[open] summary .chev{transform:rotate(180deg)}
details.think .body{padding:10px 14px 13px;color:var(--dim);font-size:13px;
  white-space:pre-wrap;word-break:break-word}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:12px}
.toolgroup{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  padding:4px 0}
.tool{display:flex;align-items:flex-start;gap:10px;padding:9px 14px}
.tool+.tool{border-top:1px solid var(--panel3)}
.ticon{flex:none;width:30px;height:30px;border-radius:8px;background:var(--panel3);
  border:1px solid var(--border);display:flex;align-items:center;justify-content:center;
  color:var(--dim)}
.tmain{flex:1;min-width:0;padding-top:1px}
.tcap{display:block;font-size:11px;font-weight:600;color:var(--dim);letter-spacing:.02em}
.ttitle{display:block;font-size:13.5px;color:var(--text);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.tright{flex:none;display:flex;align-items:center;gap:8px;padding-top:6px}
.dur{color:var(--dimmer);font-size:11.5px;font-variant-numeric:tabular-nums}
.st{display:inline-flex;align-items:center;color:var(--dimmer);font-size:11px}
.st.in_progress{color:var(--accent)}
.st.completed{color:var(--green)}
.st.failed,.st.error{color:var(--red)}
details.tooldetail{margin:-2px 14px 8px 54px}
details.tooldetail summary{list-style:none;display:inline-flex;align-items:center;gap:4px;
  cursor:pointer;color:var(--dimmer);font-size:11px}
details.tooldetail summary::-webkit-details-marker{display:none}
details.tooldetail[open] summary .chev{transform:rotate(180deg)}
details.tooldetail summary .chev{display:inline-flex}
details.tooldetail pre{background:var(--panel2);border:1px solid var(--border);
  border-radius:8px;padding:9px 11px;font:11.5px/1.5 var(--font-mono);color:var(--dim);
  overflow-x:auto;white-space:pre-wrap;word-break:break-all;max-height:240px;
  overflow-y:auto;margin:6px 0 2px}
.turnend{display:flex;align-items:center;gap:10px;color:var(--dimmer);font-size:11.5px;
  margin:20px auto;max-width:960px}
.turnend::before,.turnend::after{content:"";flex:1;height:1px;background:var(--border)}
.turnend.err{color:var(--red)}
.empty{color:var(--dimmer);text-align:center;margin-top:80px;font-size:14px}
/* ---------- composer ---------- */
#chatbar{border-top:1px solid var(--border);background:var(--panel);padding:14px 26px;
  display:flex;gap:10px;align-items:flex-end}
#chatinput{flex:1;resize:none;background:var(--panel);border:1px solid var(--border);
  border-radius:10px;color:var(--text);padding:10px 14px;font:inherit;font-size:13.5px;
  min-height:40px;max-height:160px}
#chatinput::placeholder{color:var(--dimmer)}
#chatinput:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-tint)}
#chatinput:disabled{background:var(--panel2);color:var(--dimmer)}
#chatsend{flex:none;width:40px;height:40px;border:none;border-radius:10px;
  background:var(--accent);color:var(--on-accent);display:flex;align-items:center;justify-content:center;
  cursor:pointer}
#chatsend:hover{background:var(--accent-hover)}
#chatsend:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#chatsend:disabled{opacity:.45;cursor:default}
#chatstatus{font-size:11.5px;color:var(--dim);min-width:96px;align-self:center}
#backtop{position:fixed;right:26px;bottom:26px;background:var(--panel);
  border:1px solid var(--border);color:var(--dim);border-radius:999px;padding:7px 14px;
  cursor:pointer;font-size:12px;display:none;align-items:center;gap:5px;
  box-shadow:var(--shadow)}
#backtop:focus-visible{outline:2px solid var(--accent)}
.clamp{max-height:120px;overflow:hidden;position:relative}
.clamp::after{content:"";position:absolute;bottom:0;left:0;right:0;height:40px;
  background:linear-gradient(transparent,var(--panel))}
.expand{cursor:pointer;color:var(--accent);font:inherit;font-size:12px;margin-top:6px;
  display:inline-block;background:none;border:none;padding:0}
.expand:focus-visible{outline:2px solid var(--accent)}
#backdrop{display:none}
@media (max-width:900px){
  #menubtn{display:flex}
  #sidebar{position:fixed;top:0;left:0;bottom:0;width:min(320px,85vw);z-index:40;
    transform:translateX(-105%);transition:transform .2s ease;
    box-shadow:4px 0 24px rgba(0,0,0,.15)}
  #sidebar.open{transform:none}
  #backdrop.open{display:block;position:fixed;inset:0;z-index:35;
    background:rgba(0,0,0,.35)}
  #sesshead{padding:12px 16px;min-height:0}
  #content{padding:14px 16px}
  #chatbar{padding:10px 16px}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
  .subav-wrap.pulse{animation:none}
}
</style>
</head>
<body>
<div class="app">
  <aside id="sidebar">
    <div class="side-head">
      <h2>Sub Agents</h2>
      <span id="live" aria-live="polite"><span class="ldot"></span><span class="lt">connecting…</span></span>
    </div>
    <div id="sesslist" role="listbox" aria-label="Sessions"></div>
    <div class="side-foot">
      <span id="themelbl" class="side-foot-label">Theme</span>
      <div class="theme-options" role="radiogroup" aria-labelledby="themelbl">
        <button type="button" role="radio" data-theme-pref="system" aria-checked="true"
          tabindex="0" title="Follow system">System</button>
        <button type="button" role="radio" data-theme-pref="light" aria-checked="false"
          tabindex="-1">Light</button>
        <button type="button" role="radio" data-theme-pref="dark" aria-checked="false"
          tabindex="-1">Dark</button>
      </div>
    </div>
  </aside>
  <div id="backdrop"></div>
  <div id="pane">
    <header id="sesshead">
      <button id="menubtn" type="button" aria-label="Show session list"
        aria-controls="sidebar" aria-expanded="false"><svg class="ic" width="16" height="16"
        viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M3 6h18"/><path d="M3 12h18"/><path d="M3 18h18"/></svg></button>
      <div id="hwrap"><div class="hplaceholder">Select a session</div></div>
    </header>
    <div id="content"><div class="empty">Select a session</div></div>
    <div id="chatbar">
      <textarea id="chatinput" rows="1" disabled
        placeholder="Send an instruction… (Enter to send, Shift+Enter for newline)"
        aria-label="Message the selected session"></textarea>
      <button id="chatsend" type="button" disabled aria-label="Send message" title="Send"><svg class="ic"
        width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
        focusable="false"><path d="M3 12h18m-9-9l9 9-9 9"/></svg></button>
      <span id="chatstatus" aria-live="polite"></span>
    </div>
  </div>
</div>
<button id="backtop" type="button"><svg class="ic" width="13" height="13" viewBox="0 0 24 24"
  fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true" focusable="false"><polyline points="6 9 12 15 18 9"/></svg> latest</button>
<script>
"use strict";
const $=s=>document.querySelector(s);
let sessions=[], tasks=[], selected=null;
let offsets={};            // per-session byte offset for incremental reads
let eventsCache={};        // per-session array of normalized events (for fast re-render)
let pollTimer=null, ovTimer=null;

/* ---------- presence heartbeat (bridge pops us up only when no tab is open) ---------- */
const clientId=sessionStorage.getItem("abClient")||
  (crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random());
sessionStorage.setItem("abClient",clientId);
function ping(bye){
  if(bye){try{navigator.sendBeacon(`/api/presence?id=${clientId}&bye=1`)}catch(e){}}
  else fetch(`/api/presence?id=${clientId}`).catch(()=>{});
}
ping(0);setInterval(()=>ping(0),4000);
addEventListener("pagehide",()=>ping(1));

const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtTs=t=>{try{return new Date(t).toLocaleTimeString()}catch(e){return""}};
const ago=t=>{const s=(Date.now()-new Date(t))/1e3;
  if(s<60)return Math.floor(s)+"s ago"; if(s<3600)return Math.floor(s/60)+"m ago";
  if(s<86400)return Math.floor(s/3600)+"h ago"; return Math.floor(s/86400)+"d ago"};

/* ---------- inline icons (showcase-style: stroke 24x24, filled glyphs, brand marks) ---------- */
const STROKE='fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
const ICONS={
  doc:['<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>','<path d="M14 2v6h6"/>','<path d="M8 13h8"/>','<path d="M8 17h5"/>'],
  msg:['<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'],
  monitor:['<rect x="2" y="3" width="20" height="14" rx="2"/>','<path d="M8 21h8"/>','<path d="M12 17v4"/>'],
  sun:['<circle cx="12" cy="12" r="4"/>','<path d="M12 2v2"/>','<path d="M12 20v2"/>','<path d="M4.93 4.93l1.41 1.41"/>','<path d="M17.66 17.66l1.41 1.41"/>','<path d="M2 12h2"/>','<path d="M20 12h2"/>','<path d="M6.34 17.66l-1.41 1.41"/>','<path d="M19.07 4.93l-1.41 1.41"/>'],
  moon:['<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>'],
  search:['<circle cx="11" cy="11" r="8"/>','<path d="M21 21l-4.35-4.35"/>'],
  edit:['<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>','<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'],
  external:['<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>','<polyline points="15 3 21 3 21 9"/>','<line x1="10" y1="14" x2="21" y2="3"/>'],
  gear:['<circle cx="12" cy="12" r="3"/>','<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82.33l.06.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>'],
  checkCircle:['<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>','<polyline points="22 4 12 14.01 9 11.01"/>'],
  xCircle:['<circle cx="12" cy="12" r="10"/>','<line x1="15" y1="9" x2="9" y2="15"/>','<line x1="9" y1="9" x2="15" y2="15"/>'],
  hourglass:['<path d="M12 12C15.333 12 17 9 17 3H7C7 9 8.667 12 12 12ZM12 12C15.333 12 17 15 17 21H7C7 15 8.667 12 12 12Z"/>'],
  clock:['<circle cx="12" cy="12" r="10"/>','<path d="M12 6v6l4 2"/>'],
  chevron:['<polyline points="6 9 12 15 18 9"/>'],
  arrow:['<path d="M3 12h18m-9-9l9 9-9 9"/>'],
  sparkle:['<path d="M12 2C13 7 17 11 22 12C17 13 13 17 12 22C11 17 7 13 2 12C7 11 11 7 12 2Z"/>','fill'],
  arc:['<path opacity=".3" d="M18 12C18 8.68629 15.3137 6 12 6C8.68629 6 6 8.68629 6 12C6 15.3137 8.68629 18 12 18C15.3137 18 18 15.3137 18 12ZM20 12C20 16.4183 16.4183 20 12 20C7.58172 20 4 16.4183 4 12C4 7.58172 7.58172 4 12 4C16.4183 4 20 7.58172 20 12Z"/>','<path d="M12 4C16.4183 4 20 7.58172 20 12C20 16.4183 16.4183 20 12 20C7.58172 20 4 16.4183 4 12H6C6 15.3137 8.68629 18 12 18C15.3137 18 18 15.3137 18 12C18 8.68629 15.3137 6 12 6V4Z"/>','fill'],
  dot:['<circle cx="12" cy="12" r="6"/>','fill'],
};
const BRANDS={
  devin:{vb:"0 0 500 500",html:'<path fill="#2A6DCE" d="M59.29,209.39l48.87,28.21c1.75,1.01,3.71,1.51,5.67,1.51c1.95,0,3.92-0.52,5.67-1.51l48.87-28.21 c0,0,0.14-0.11,0.2-0.16c0.74-0.45,1.44-0.99,2.07-1.6c0.09-0.09,0.18-0.2,0.27-0.29c0.54-0.58,1.03-1.21,1.44-1.89 c0.06-0.11,0.16-0.2,0.2-0.32c0.43-0.74,0.74-1.53,0.99-2.37c0.05-0.18,0.09-0.36,0.14-0.54c0.2-0.86,0.36-1.74,0.36-2.66v-28.21 c0-10.89,5.87-21.03,15.3-26.48c9.42-5.45,21.15-5.44,30.59,0l24.43,14.11c0.79,0.45,1.62,0.77,2.47,1.01 c0.18,0.05,0.37,0.11,0.54,0.16c0.83,0.2,1.69,0.32,2.54,0.34c0.05,0,0.09,0,0.11,0c0.09,0,0.18-0.05,0.26-0.05 c0.79,0,1.58-0.11,2.34-0.32c0.14-0.03,0.27-0.05,0.4-0.09c0.83-0.23,1.64-0.57,2.41-0.99c0.06-0.05,0.16-0.05,0.23-0.09 l48.87-28.21c3.51-2.03,5.67-5.76,5.67-9.81V64.52c0-4.05-2.16-7.78-5.67-9.81l-48.91-28.19c-3.51-2.03-7.81-2.03-11.32,0 l-48.87,28.21c0,0-0.14,0.11-0.2,0.16c-0.74,0.45-1.44,0.99-2.07,1.6c0.09-0.09,0.18-0.2,0.27-0.29 c-0.54,0.58-1.03,1.21-1.44,1.89c-0.06,0.11-0.16,0.2-0.2,0.31c-0.43,0.74-0.74,1.53-0.99,2.37c-0.05,0.18-0.09,0.36-0.14,0.54 c-0.2,0.86-0.36,1.74-0.36,2.66v28.21c0,10.89-5.87,21.03-15.3,26.5c-9.42,5.44-21.15,5.44-30.59,0l-24.42-14.1 c-0.79-0.45-1.63-0.77-2.47-1.01c-0.18-0.05-0.36-0.11-0.54-0.16c-0.84-0.2-1.69-0.31-2.55-0.34c-0.14,0-0.25,0-0.38,0 c-0.81,0-1.6,0.11-2.37,0.31c-0.14,0.02-0.25,0.05-0.38,0.09c-0.82,0.23-1.63,0.57-2.4,1c-0.06,0.05-0.16,0.05-0.23,0.09 l-48.84,28.24c-3.51,2.03-5.67,5.76-5.67,9.81v56.42c0,4.05,2.16,7.78,5.67,9.81C59.29,209.41,59.29,209.39,59.29,209.39z"/><path fill="#1DC19C" d="M325.46,223.49c9.42-5.44,21.15-5.44,30.59,0l24.43,14.11c0.79,0.45,1.62,0.77,2.47,1.01 c0.18,0.05,0.36,0.11,0.54,0.16c0.83,0.2,1.69,0.31,2.54,0.34c0.05,0,0.09,0,0.11,0c0.09,0,0.18-0.03,0.26-0.05 c0.79,0,1.58-0.11,2.34-0.31c0.14-0.03,0.27-0.05,0.4-0.09c0.83-0.23,1.62-0.57,2.41-0.99c0.06-0.05,0.16-0.05,0.25-0.09 l48.87-28.21c3.51-2.03,5.67-5.76,5.67-9.81v-56.43c0-4.05-2.16-7.78-5.67-9.81l-48.84-28.22c-3.51-2.03-7.81-2.03-11.32,0 l-48.87,28.21c0,0-0.14,0.11-0.2,0.16c-0.74,0.45-1.44,0.99-2.07,1.6c0.09-0.09,0.18-0.2,0.26-0.29 c-0.54,0.58-1.03,1.21-1.44,1.89c-0.06,0.11-0.16,0.2-0.2,0.32c-0.43-0.74-0.74,1.53-0.99,2.37c-0.05,0.18-0.09,0.36-0.14,0.54 c-0.2,0.86-0.36,1.74-0.36,2.66v28.21c0,10.89-5.87,21.03-15.3,26.5c-9.42,5.44-21.15,5.44-30.59,0l-24.43-14.11 c-0.79-0.45-1.62-0.77-2.47-1.01c-0.18-0.05-0.36-0.11-0.54-0.16c-0.83-0.2-1.69-0.32-2.54-0.34c-0.14,0-0.25,0-0.38,0 c-0.81,0-1.6,0.11-2.37,0.32c-0.14,0.03-0.25,0.05-0.38,0.09c-0.83,0.23-1.64,0.57-2.41,0.99c-0.06,0.05-0.16,0.05-0.23,0.09 l-48.87,28.21c-3.51,2.03-5.67,5.76-5.67,9.81v56.43c0,4.05,2.16,7.78,5.67,9.81l48.87,28.21c0,0,0.16,0.05,0.23,0.09 c0.77,0.43,1.58,0.77,2.41,0.99c0.14,0.05,0.27,0.05,0.4,0.09c0.77,0.18,1.55,0.29,2.34,0.32c0.09,0,0.18,0.05,0.27,0.05 c0.05,0,0.09,0,0.11,0c0.86,0,1.69-0.14,2.54-0.34c0.18-0.05,0.36-0.09,0.54-0.16c0.86-0.25,1.69-0.57,2.47-1.01l24.43-14.11 c9.42-5.44,21.15-5.44,30.59,0c9.42,5.44,15.3,15.59,15.3,26.48v28.21c0,0.92,0.14,1.8,0.36,2.66c0.05,0.18,0.09,0.36,0.14,0.54 c0.25,0.83,0.56,1.62,0.99,2.37c0.06,0.11,0.14,0.2,0.2,0.31c0.4,0.68,0.9,1.31,1.44,1.89c0.09,0.09,0.18,0.2,0.26,0.29 c0.61,0.6,1.31,1.12,2.07,1.6c0.06,0.05,0.11,0.11,0.2,0.16l48.87,28.21c1.75,1.01,3.72,1.51,5.67,1.51s3.92-0.52,5.67-1.51 l48.87-28.21c3.51-2.03,5.67-5.76,5.67-9.81v-56.43c0-4.05-2.16-7.78-5.67-9.81l-48.87-28.21c0,0-0.16-0.05-0.23-0.09 c-0.77-0.43-1.58-0.77-2.41-0.99c-0.14-0.05-0.25-0.05-0.38-0.09c-0.79-0.18-1.57-0.29-2.38-0.32c-0.11,0-0.25,0-0.36,0 c-0.86,0-1.71,0.14-2.54,0.34c-0.18,0.05-0.34,0.09-0.52,0.16c-0.86,0.25-1.69,0.57-2.47,1.01l-24.43,14.11 c-9.42,5.44-21.15,5.44-30.58,0c-9.42-5.44-15.3-15.59-15.3-26.5c0-10.91,5.87-21.03,15.3-26.48 C325.55,223.49,325.46,223.49,325.46,223.49z"/><path fill="#1796E2" d="M304.5,369.22l-48.87-28.21c0,0-0.16-0.05-0.23-0.09c-0.77-0.43-1.57-0.77-2.41-0.99 c-0.14-0.05-0.27-0.05-0.4-0.09c-0.79-0.18-1.57-0.29-2.37-0.32c-0.14,0-0.25,0-0.38,0c-0.86,0-1.71,0.14-2.54,0.34 c-0.18,0.05-0.34,0.09-0.52,0.16c-0.86,0.25-1.69,0.57-2.47,1.01l-24.43,14.11c-9.42,5.44-21.15,5.44-30.58,0 c-9.42-5.44-15.3-15.59-15.3-26.5v-28.22c0-0.92-0.14-1.8-0.36-2.66c-0.05-0.18-0.09-0.36-0.14-0.54 c-0.25-0.83-0.57-1.62-0.99-2.37c-0.06-0.11-0.14-0.2-0.2-0.32c-0.4-0.68-0.9-1.31-1.44-1.89c-0.09-0.09-0.18-0.2-0.27-0.29 c-0.6-0.6-1.31-1.12-2.07-1.6c-0.06-0.05-0.11-0.11-0.2-0.16l-48.87-28.21c-3.51-2.03-7.81-2.03-11.32,0L59.28,290.6 c-3.51,2.03-5.67,5.76-5.67,9.81v56.43c0,4.05,2.16,7.78,5.67,9.81l48.87,28.21c0,0,0.16,0.06,0.23,0.09 c0.77,0.43,1.55,0.77,2.38,0.99c0.14,0.05,0.27,0.06,0.4,0.09c0.77,0.18,1.55,0.29,2.34,0.32c0.09,0,0.18,0.05,0.29,0.05 c0.05,0,0.09,0,0.14,0c0.86,0,1.69-0.14,2.52-0.34c0.18-0.05,0.36-0.09,0.54-0.16c0.86-0.25,1.69-0.57,2.47-1.01l24.43-14.11 c9.42-5.44,21.15-5.44,30.59,0c9.42,5.44,15.3,15.59,15.3,26.48v28.21c0,0.92,0.14,1.8,0.36,2.66c0.05,0.18,0.09,0.36,0.14,0.54 c0.25,0.83,0.57,1.62,0.99,2.37c0.06,0.11,0.14,0.2,0.2,0.32c0.4,0.68,0.9,1.31,1.44,1.89c0.09,0.09,0.18,0.2,0.27,0.29 c0.61,0.61,1.31,1.12,2.07,1.6c0.06,0.05,0.11,0.11,0.2,0.16l48.87,28.21c1.75,1.01,3.71,1.51,5.67,1.51 c1.96,0,3.92-0.52,5.67-1.51l48.87-28.21c3.51-2.03,5.67-5.76,5.67-9.81v-56.43c0-4.05-2.16-7.78-5.67-9.81L304.5,369.22z"/>'},
  claude:{vb:"1 2 46 44",html:'<defs><linearGradient id="claudeG" x1="24" x2="24" y1="2.987" y2="45.013" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#d97757"/><stop offset="1" stop-color="#db5b32"/></linearGradient></defs><path fill="url(#claudeG)" d="M11.239,30.934l8.264-4.637l0.139-0.403l-0.139-0.224h-0.403l-1.381-0.085l-4.722-0.128l-4.095-0.17l-3.968-0.213l-0.998-0.213 L3,23.628l0.096-0.615l0.839-0.564l1.203,0.105l2.657,0.182l3.988,0.275l2.893,0.17l4.285,0.445h0.681l0.096-0.275l-0.233-0.17 l-0.182-0.17l-4.127-2.796l-4.467-2.955l-2.34-1.702l-1.265-0.862l-0.638-0.808l-0.275-1.764l1.149-1.265l1.543,0.105l0.394,0.105 l1.563,1.203l3.338,2.584l4.359,3.21l0.638,0.53l0.255-0.182l0.031-0.128l-0.286-0.479l-2.371-4.285l-2.53-4.359L13.17,7.355 l-0.298-1.083c-0.105-0.445-0.182-0.82-0.182-1.276l1.307-1.775l0.723-0.233l1.744,0.233L17.2,3.858l1.083,2.479l1.756,3.902 l2.723,5.306l0.797,1.574l0.425,1.458l0.159,0.445h0.275v-0.255l0.224-2.989l0.414-3.67l0.403-4.722l0.139-1.33l0.658-1.594 L27.564,3.6l1.021,0.488l0.839,1.203l-0.116,0.777l-0.499,3.245l-0.978,5.082l-0.638,3.403h0.372l0.425-0.425l1.722-2.286 l2.893-3.616l1.276-1.435l1.489-1.585l0.956-0.754h1.807l1.33,1.977l-0.596,2.042l-1.86,2.36l-1.543,1.999l-2.212,2.978 l-1.381,2.382l0.128,0.19l0.329-0.031l4.997-1.064l2.7-0.488l3.222-0.553l1.458,0.681l0.159,0.692l-0.573,1.415l-3.446,0.851 l-4.041,0.808l-6.018,1.424l-0.074,0.054l0.085,0.105l2.711,0.255l1.16,0.062h2.839l5.287,0.394l1.381,0.913L45,28.26l-0.139,0.851 l-2.127,1.083l-2.87-0.681l-6.699-1.594l-2.297-0.573H30.55v0.19l1.914,1.872l3.508,3.168l4.393,4.084l0.224,1.01l-0.564,0.797 l-0.596-0.085l-3.86-2.904l-1.489-1.307l-3.372-2.839h-0.224v0.298l0.777,1.137l4.104,6.169l0.213,1.892l-0.298,0.615l-1.064,0.372 l-1.168-0.213l-2.402-3.372l-2.479-3.798l-1.999-3.403l-0.244,0.139l-1.18,12.709l-0.553,0.649l-1.276,0.488l-1.064-0.808 l-0.564-1.307l0.564-2.584l0.681-3.372l0.553-2.68l0.499-3.33l0.298-1.106l-0.02-0.074l-0.244,0.031l-2.51,3.446l-3.817,5.159 l-3.02,3.233l-0.723,0.286l-1.254-0.649l0.116-1.16l0.701-1.032l4.18-5.318l2.521-3.296l1.628-1.903l-0.011-0.275h-0.096 l-11.103,7.209l-1.977,0.255l-0.851-0.797l0.105-1.307l0.403-0.425l3.338-2.297l-0.011,0.011L11.239,30.934z"/>'},
  codex:{vb:"93.557 94.958 534.139 529.818",html:'<path fill="currentColor" d="M304.246 294.611V249.028C304.246 245.189 305.687 242.309 309.044 240.392L400.692 187.612C413.167 180.415 428.042 177.058 443.394 177.058C500.971 177.058 537.44 221.682 537.44 269.182C537.44 272.54 537.44 276.379 536.959 280.218L441.954 224.558C436.197 221.201 430.437 221.201 424.68 224.558L304.246 294.611ZM518.245 472.145V363.224C518.245 356.505 515.364 351.707 509.608 348.349L389.174 278.296L428.519 255.743C431.877 253.826 434.757 253.826 438.115 255.743L529.762 308.523C556.154 323.879 573.905 356.505 573.905 388.171C573.905 424.636 552.315 458.225 518.245 472.141V472.145ZM275.937 376.182L236.592 353.152C233.235 351.235 231.794 348.354 231.794 344.515V238.956C231.794 187.617 271.139 148.749 324.4 148.749C344.555 148.749 363.264 155.468 379.102 167.463L284.578 222.164C278.822 225.521 275.942 230.319 275.942 237.039V376.186L275.937 376.182ZM360.626 425.122L304.246 393.455V326.283L360.626 294.616L417.002 326.283V393.455L360.626 425.122ZM396.852 570.989C376.698 570.989 357.989 564.27 342.151 552.276L436.674 497.574C442.431 494.217 445.311 489.419 445.311 482.699V343.552L485.138 366.582C488.495 368.499 489.936 371.379 489.936 375.219V480.778C489.936 532.117 450.109 570.985 396.852 570.985V570.989ZM283.134 463.99L191.486 411.211C165.094 395.854 147.343 363.229 147.343 331.562C147.343 294.616 169.415 261.509 203.48 247.593V356.991C203.48 363.71 206.361 368.508 212.117 371.866L332.074 441.437L292.729 463.99C289.372 465.907 286.491 465.907 283.134 463.99ZM277.859 542.68C223.639 542.68 183.813 501.895 183.813 451.514C183.813 447.675 184.294 443.836 184.771 439.997L279.295 494.698C285.051 498.056 290.812 498.056 296.568 494.698L417.002 425.127V470.71C417.002 474.549 415.562 477.429 412.204 479.346L320.557 532.126C308.081 539.323 293.206 542.68 277.854 542.68H277.859ZM396.852 599.776C454.911 599.776 503.37 558.513 514.41 503.812C568.149 489.896 602.696 439.515 602.696 388.176C602.696 354.587 588.303 321.962 562.392 298.45C564.791 288.373 566.231 278.296 566.231 268.224C566.231 199.611 510.571 148.267 446.274 148.267C433.322 148.267 420.846 150.184 408.37 154.505C386.775 133.392 357.026 119.958 324.4 119.958C266.342 119.958 217.883 161.22 206.843 215.921C153.104 229.837 118.557 280.218 118.557 331.557C118.557 365.146 132.95 397.771 158.861 421.283C156.462 431.36 155.022 441.437 155.022 451.51C155.022 520.123 210.682 571.466 274.978 571.466C287.931 571.466 300.407 569.549 312.883 565.228C334.473 586.341 364.222 599.776 396.852 599.776Z"/>'},
  cursor:{vb:"0 0 24 24",html:'<path d="M11.925 24l10.425-6-10.425-6L1.5 18l10.425 6z" fill="currentColor" opacity="0.6"/><path d="M22.35 18V6L11.925 0v12l10.425 6z" fill="currentColor" opacity="0.2"/><path d="M11.925 0L1.5 6v12l10.425-6V0z" fill="currentColor" opacity="0.4"/><path d="M22.35 6L11.925 24V12L22.35 6z" fill="currentColor" opacity="0.3"/><path d="M22.35 6l-10.425 6L1.5 6h20.85z" fill="currentColor"/>'},
};
function icon(name,size=14){
  const b=BRANDS[name];
  if(b)return `<svg class="ic" width="${size}" height="${size}" viewBox="${b.vb}" aria-hidden="true" focusable="false">${b.html}</svg>`;
  const def=ICONS[name]||ICONS.dot,fill=def.includes("fill");
  const inner=def.filter(d=>d!=="fill").join("");
  return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" `+
    (fill?'fill="currentColor" stroke="none"':STROKE)+` aria-hidden="true" focusable="false">${inner}</svg>`;
}
function spin(){return '<span class="spin" aria-hidden="true">'+icon("arc",11)+'</span>'}
function agentIcon(agent,size){
  const a=(agent||"").toLowerCase();
  if(a==="devin")return icon("devin",size);
  if(a.startsWith("claude"))return icon("claude",size);
  if(a.startsWith("codex"))return icon("codex",size);
  if(a==="cursor")return icon("cursor",size);
  return icon("monitor",size);
}

/* ---------- seeded subagent avatars (Codex-style gradient glyphs) ---------- */
const SUBAV=[
  {l:"data:image/svg+xml,%3csvg%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%20fill='none'%20xmlns='http://www.w3.org/2000/svg'%3e%3cg%20clip-path='url(%23clip0_50_136)'%3e%3cpath%20opacity='0.6'%20d='M7.00314%2014.9285C7.5498%2015.0071%207.99979%2014.5521%207.99979%2013.9998V8H2C1.44772%208%200.992659%208.44999%201.07129%208.99665C1.28531%2010.4846%201.97469%2011.8741%203.05019%2012.9496C4.12569%2014.0251%205.51514%2014.7145%207.00314%2014.9285Z'%20fill='url(%23paint0_linear_50_136)'%20stroke='white'%20stroke-width='0.5'/%3e%3cpath%20d='M7.99979%2012.3666V8H3.63316C3.63316%209.1581%204.09322%2010.2688%204.91212%2011.0877C5.73102%2011.9066%206.84169%2012.3666%207.99979%2012.3666Z'%20fill='url(%23paint1_linear_50_136)'%20stroke='white'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M8.99686%201.0715C8.4502%200.992869%208.00021%201.44793%208.00021%202.00021L8.00021%208L14%208C14.5523%208%2015.0073%207.55001%2014.9287%207.00335C14.7147%205.51536%2014.0253%204.12591%2012.9498%203.0504C11.8743%201.9749%2010.4849%201.28552%208.99686%201.0715Z'%20fill='url(%23paint2_linear_50_136)'%20stroke='white'%20stroke-width='0.5'/%3e%3cpath%20d='M8.00021%203.63337L8.00021%208L12.3668%208C12.3668%206.8419%2011.9068%205.73123%2011.0879%204.91233C10.269%204.09343%209.15831%203.63337%208.00021%203.63337Z'%20fill='url(%23paint3_linear_50_136)'%20stroke='white'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M2.41888%207.50046C2.39428%207.7755%202.62045%207.99984%202.8966%207.99984L7.99979%207.99984L7.99979%202.89665C7.99979%202.62051%207.77545%202.39433%207.5004%202.41893C6.19743%202.53548%204.97018%203.10534%204.03773%204.03779C3.10529%204.97023%202.53542%206.19748%202.41888%207.50046Z'%20fill='url(%23paint4_linear_50_136)'%20stroke='white'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M13.7524%208.49959C13.7763%208.22448%2013.5502%208.00016%2013.274%208.00016L7.99979%208.00016L7.99979%2013.2744C7.99979%2013.5505%208.2241%2013.7767%208.49921%2013.7528C9.84762%2013.6358%2011.1183%2013.0477%2012.0828%2012.0832C13.0473%2011.1186%2013.6354%209.848%2013.7524%208.49959Z'%20fill='url(%23paint5_linear_50_136)'%20stroke='white'%20stroke-width='0.5'/%3e%3c/g%3e%3cdefs%3e%3clinearGradient%20id='paint0_linear_50_136'%20x1='2.42183'%20y1='8.98434'%20x2='7.01545'%20y2='14.3436'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint1_linear_50_136'%20x1='4.52013'%20y1='8.61406'%20x2='7.38573'%20y2='11.9573'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint2_linear_50_136'%20x1='13.5782'%20y1='7.01566'%20x2='8.98455'%20y2='1.65644'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint3_linear_50_136'%20x1='11.4799'%20y1='7.38595'%20x2='8.61427'%20y2='4.04275'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint4_linear_50_136'%20x1='7.21184'%20y1='3.5348'%20x2='2.9219'%20y2='7.21189'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint5_linear_50_136'%20x1='8.81179'%20y1='12.6015'%20x2='13.2327'%20y2='8.81217'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3cclipPath%20id='clip0_50_136'%3e%3crect%20width='16'%20height='16'%20fill='white'/%3e%3c/clipPath%3e%3c/defs%3e%3c/svg%3e",
   d:"data:image/svg+xml,%3csvg%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%20fill='none'%20xmlns='http://www.w3.org/2000/svg'%3e%3cg%20clip-path='url(%23clip0_51_256)'%3e%3cpath%20opacity='0.6'%20d='M7.00314%2014.9285C7.5498%2015.0071%207.99979%2014.5521%207.99979%2013.9998V8H2C1.44772%208%200.992659%208.44999%201.07129%208.99665C1.28531%2010.4846%201.97469%2011.8741%203.05019%2012.9496C4.12569%2014.0251%205.51514%2014.7145%207.00314%2014.9285Z'%20fill='url(%23paint0_linear_51_256)'%20stroke='%23033F43'%20stroke-width='0.5'/%3e%3cpath%20d='M7.99979%2012.3666V8H3.63316C3.63316%209.1581%204.09322%2010.2688%204.91212%2011.0877C5.73102%2011.9066%206.84169%2012.3666%207.99979%2012.3666Z'%20fill='url(%23paint1_linear_51_256)'%20stroke='%23033F43'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M8.99686%201.0715C8.4502%200.992869%208.00021%201.44793%208.00021%202.00021L8.00021%208L14%208C14.5523%208%2015.0073%207.55001%2014.9287%207.00335C14.7147%205.51536%2014.0253%204.12591%2012.9498%203.0504C11.8743%201.9749%2010.4849%201.28552%208.99686%201.0715Z'%20fill='url(%23paint2_linear_51_256)'%20stroke='%23033F43'%20stroke-width='0.5'/%3e%3cpath%20d='M8.00021%203.63337L8.00021%208L12.3668%208C12.3668%206.8419%2011.9068%205.73123%2011.0879%204.91233C10.269%204.09343%209.15831%203.63337%208.00021%203.63337Z'%20fill='url(%23paint3_linear_51_256)'%20stroke='%23033F43'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M2.41888%207.50046C2.39428%207.7755%202.62045%207.99984%202.8966%207.99984L7.99979%207.99984L7.99979%202.89665C7.99979%202.62051%207.77545%202.39433%207.5004%202.41893C6.19743%202.53548%204.97018%203.10534%204.03773%204.03779C3.10529%204.97023%202.53542%206.19748%202.41888%207.50046Z'%20fill='url(%23paint4_linear_51_256)'%20stroke='%23033F43'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M13.7524%208.49959C13.7763%208.22448%2013.5502%208.00017%2013.274%208.00017L7.99979%208.00016L7.99979%2013.2744C7.99979%2013.5505%208.2241%2013.7767%208.49921%2013.7528C9.84762%2013.6358%2011.1183%2013.0477%2012.0828%2012.0832C13.0473%2011.1186%2013.6354%209.848%2013.7524%208.49959Z'%20fill='url(%23paint5_linear_51_256)'%20stroke='%23033F43'%20stroke-width='0.5'/%3e%3c/g%3e%3cdefs%3e%3clinearGradient%20id='paint0_linear_51_256'%20x1='2.42183'%20y1='8.98435'%20x2='7.01545'%20y2='14.3436'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint1_linear_51_256'%20x1='4.52013'%20y1='8.61406'%20x2='7.38573'%20y2='11.9573'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint2_linear_51_256'%20x1='13.5782'%20y1='7.01566'%20x2='8.98455'%20y2='1.65644'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint3_linear_51_256'%20x1='11.4799'%20y1='7.38595'%20x2='8.61427'%20y2='4.04275'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint4_linear_51_256'%20x1='7.21184'%20y1='3.5348'%20x2='2.9219'%20y2='7.21189'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint5_linear_51_256'%20x1='8.81179'%20y1='12.6015'%20x2='13.2327'%20y2='8.81217'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3cclipPath%20id='clip0_51_256'%3e%3crect%20width='16'%20height='16'%20fill='white'/%3e%3c/clipPath%3e%3c/defs%3e%3c/svg%3e"},
  {l:"data:image/svg+xml,%3csvg%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%20fill='none'%20xmlns='http://www.w3.org/2000/svg'%3e%3cpath%20d='M15%208C15%2011.866%2011.866%2015%208%2015C4.13401%2015%201%2011.866%201%208C1%204.13401%204.13401%201%208%201C11.866%201%2015%204.13401%2015%208Z'%20fill='url(%23paint0_linear_50_132)'/%3e%3cpath%20opacity='0.8'%20d='M7.99985%204.85127C10.7333%204.85127%2012.949%207.0671%2012.9491%209.80049C12.9491%2012.5339%2010.7333%2014.7497%207.99985%2014.7497C5.26646%2014.7496%203.05063%2012.5339%203.05063%209.80049C3.05071%207.06715%205.2665%204.85135%207.99985%204.85127Z'%20fill='%2391E8C7'%20stroke='white'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M8.00029%208.68222C9.67572%208.68238%2011.0335%2010.0409%2011.0335%2011.7164C11.0333%2013.3917%209.67563%2014.7494%208.00029%2014.7496C6.32482%2014.7496%204.96627%2013.3918%204.96611%2011.7164C4.96611%2010.0408%206.32472%208.68222%208.00029%208.68222Z'%20fill='url(%23paint1_linear_50_132)'%20stroke='white'%20stroke-width='0.5'/%3e%3cdefs%3e%3clinearGradient%20id='paint0_linear_50_132'%20x1='3.84375'%20y1='2.96875'%20x2='13.0312'%20y2='13.6875'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2391E8C7'/%3e%3cstop%20offset='1'%20stop-color='%2342BA96'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint1_linear_50_132'%20x1='6.05019'%20y1='9.35582'%20x2='10.3603'%20y2='14.3843'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2391E8C7'/%3e%3cstop%20offset='1'%20stop-color='%2342BA96'/%3e%3c/linearGradient%3e%3c/defs%3e%3c/svg%3e",
   d:"data:image/svg+xml,%3csvg%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%20fill='none'%20xmlns='http://www.w3.org/2000/svg'%3e%3cpath%20d='M8%201.25C11.7279%201.25%2014.75%204.27208%2014.75%208C14.75%2011.7279%2011.7279%2014.75%208%2014.75C4.27208%2014.75%201.25%2011.7279%201.25%208C1.25%204.27208%204.27208%201.25%208%201.25Z'%20fill='url(%23paint0_linear_51_252)'%20stroke='%231A5B40'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.8'%20d='M7.99985%204.85127C10.7333%204.85127%2012.949%207.0671%2012.9491%209.80049C12.9491%2012.5339%2010.7333%2014.7497%207.99985%2014.7497C5.26646%2014.7496%203.05063%2012.5339%203.05063%209.80049C3.05071%207.06715%205.2665%204.85135%207.99985%204.85127Z'%20fill='%2391E8C7'%20stroke='%231A5B40'%20stroke-width='0.5'/%3e%3cpath%20opacity='0.6'%20d='M8.00029%208.68222C9.67572%208.68238%2011.0335%2010.0409%2011.0335%2011.7164C11.0333%2013.3917%209.67563%2014.7494%208.00029%2014.7496C6.32482%2014.7496%204.96627%2013.3918%204.96611%2011.7164C4.96611%2010.0408%206.32472%208.68222%208.00029%208.68222Z'%20fill='url(%23paint1_linear_51_252)'%20stroke='%231A5B40'%20stroke-width='0.5'/%3e%3cdefs%3e%3clinearGradient%20id='paint0_linear_51_252'%20x1='3.84375'%20y1='2.96875'%20x2='13.0312'%20y2='13.6875'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2391E8C7'/%3e%3cstop%20offset='1'%20stop-color='%2342BA96'/%3e%3c/linearGradient%3e%3clinearGradient%20id='paint1_linear_51_252'%20x1='6.05019'%20y1='9.35581'%20x2='10.3603'%20y2='14.3843'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2391E8C7'/%3e%3cstop%20offset='1'%20stop-color='%2342BA96'/%3e%3c/linearGradient%3e%3c/defs%3e%3c/svg%3e"},
  {l:"data:image/svg+xml,%3csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20fill='none'%20viewBox='0%200%2016%2016'%3e%3cg%20clip-path='url(%23a)'%3e%3cpath%20fill='url(%23b)'%20stroke='%23fff'%20stroke-width='.5'%20d='M8%201.079c.738%200%201.338.598%201.338%201.337v11.168a1.338%201.338%200%200%201-2.675%200V2.416c0-.738.599-1.337%201.337-1.337Z'%20opacity='.7'/%3e%3cpath%20fill='url(%23c)'%20stroke='%23fff'%20stroke-width='.5'%20d='M12.894%203.106c.522.522.522%201.37%200%201.891l-7.897%207.897a1.338%201.338%200%200%201-1.891-1.891l7.897-7.897a1.337%201.337%200%200%201%201.89%200Z'%20opacity='.7'/%3e%3cpath%20fill='url(%23d)'%20stroke='%23fff'%20stroke-width='.5'%20d='M14.92%208c0%20.738-.598%201.338-1.336%201.338H2.416a1.338%201.338%200%200%201%200-2.675h11.168c.738%200%201.337.598%201.337%201.337Z'%20opacity='.7'/%3e%3cpath%20fill='url(%23e)'%20stroke='%23fff'%20stroke-width='.5'%20d='M12.895%2012.894a1.34%201.34%200%200%201-1.892%200L3.106%204.999a1.338%201.338%200%200%201%201.892-1.892l7.897%207.897a1.337%201.337%200%200%201%200%201.89Z'%20opacity='.7'/%3e%3c/g%3e%3cdefs%3e%3clinearGradient%20id='b'%20x1='8'%20x2='8'%20y1='.406'%20y2='15.42'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3clinearGradient%20id='c'%20x1='13.369'%20x2='2.753'%20y1='2.63'%20y2='13.247'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3clinearGradient%20id='d'%20x1='15.37'%20x2='8.291'%20y1='8'%20y2='8'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3clinearGradient%20id='e'%20x1='13.37'%20x2='2.754'%20y1='13.37'%20y2='2.753'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3cclipPath%20id='a'%3e%3cpath%20fill='%23fff'%20d='M0%200h16v16H0z'/%3e%3c/clipPath%3e%3c/defs%3e%3c/svg%3e",
   d:"data:image/svg+xml,%3csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20fill='none'%20viewBox='0%200%2016%2016'%3e%3cg%20clip-path='url(%23a)'%3e%3cpath%20fill='url(%23b)'%20stroke='%23A07E20'%20stroke-width='.5'%20d='M8%201.079c.738%200%201.338.598%201.338%201.337v11.168a1.338%201.338%200%200%201-2.675%200V2.416c0-.738.599-1.337%201.337-1.337Z'%20opacity='.7'/%3e%3cpath%20fill='url(%23c)'%20stroke='%23A07E20'%20stroke-width='.5'%20d='M12.894%203.106c.522.522.522%201.37%200%201.891l-7.897%207.897a1.338%201.338%200%200%201-1.891-1.891l7.897-7.897a1.337%201.337%200%200%201%201.89%200Z'%20opacity='.7'/%3e%3cpath%20fill='url(%23d)'%20stroke='%23A07E20'%20stroke-width='.5'%20d='M14.92%208c0%20.738-.598%201.338-1.336%201.338H2.416a1.338%201.338%200%200%201%200-2.675h11.168c.738%200%201.337.598%201.337%201.337Z'%20opacity='.7'/%3e%3cpath%20fill='url(%23e)'%20stroke='%23A07E20'%20stroke-width='.5'%20d='M12.895%2012.894a1.34%201.34%200%200%201-1.892%200L3.106%204.999a1.338%201.338%200%200%201%201.892-1.892l7.897%207.897a1.337%201.337%200%200%201%200%201.89Z'%20opacity='.7'/%3e%3c/g%3e%3cdefs%3e%3clinearGradient%20id='b'%20x1='8'%20x2='8'%20y1='.406'%20y2='15.42'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3clinearGradient%20id='c'%20x1='13.369'%20x2='2.753'%20y1='2.63'%20y2='13.247'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3clinearGradient%20id='d'%20x1='15.594'%20x2='.58'%20y1='8'%20y2='8'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3clinearGradient%20id='e'%20x1='13.37'%20x2='2.754'%20y1='13.37'%20y2='2.753'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%23FFE742'/%3e%3cstop%20offset='.428'%20stop-color='%23FFE236'/%3e%3cstop%20offset='.817'%20stop-color='%23FFCD0F'/%3e%3c/linearGradient%3e%3cclipPath%20id='a'%3e%3cpath%20fill='%23fff'%20d='M0%200h16v16H0z'/%3e%3c/clipPath%3e%3c/defs%3e%3c/svg%3e"},
  {l:"data:image/svg+xml,%3csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20fill='none'%20viewBox='0%200%2016%2016'%3e%3cpath%20fill='url(%23a)'%20stroke='%23fff'%20stroke-width='.5'%20d='M7.34%204.108a.75.75%200%200%201%201.32%200l4.83%208.917a.75.75%200%200%201-.66%201.108H3.17a.75.75%200%200%201-.66-1.108z'%20opacity='.5'/%3e%3cpath%20fill='url(%23b)'%20stroke='%23fff'%20stroke-width='.5'%20d='M8.66%2011.892a.75.75%200%200%201-1.32%200L2.51%202.975a.75.75%200%200%201%20.66-1.108h9.66a.75.75%200%200%201%20.66%201.108z'%20opacity='.5'/%3e%3cdefs%3e%3clinearGradient%20id='a'%20x1='4.136'%20x2='11.902'%20y1='4.056'%20y2='13.871'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='b'%20x1='11.864'%20x2='4.098'%20y1='11.944'%20y2='2.129'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3c/defs%3e%3c/svg%3e",
   d:"data:image/svg+xml,%3csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20fill='none'%20viewBox='0%200%2016%2016'%3e%3cpath%20fill='url(%23a)'%20stroke='%23033F43'%20stroke-width='.5'%20d='M2.29%2012.907%207.122%203.99a1%201%200%200%201%201.759%200l4.83%208.917a1%201%200%200%201-.88%201.476H3.17a1%201%200%200%201-.88-1.476Z'%20opacity='.7'/%3e%3cpath%20fill='url(%23b)'%20stroke='%23033F43'%20stroke-width='.5'%20d='m13.71%203.093-4.831%208.918a1%201%200%200%201-1.759%200L2.29%203.093a1%201%200%200%201%20.88-1.476h9.66a1%201%200%200%201%20.88%201.476Z'%20opacity='.7'/%3e%3cdefs%3e%3clinearGradient%20id='a'%20x1='4.136'%20x2='11.902'%20y1='4.056'%20y2='13.871'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3clinearGradient%20id='b'%20x1='11.864'%20x2='4.098'%20y1='11.944'%20y2='2.129'%20gradientUnits='userSpaceOnUse'%3e%3cstop%20stop-color='%2375D9E9'/%3e%3cstop%20offset='1'%20stop-color='%231895AA'/%3e%3c/linearGradient%3e%3c/defs%3e%3c/svg%3e"},
];
function subSeed(s){let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))%2147483647;return h}
function agentAvatar(s,size){
  const seed=subSeed(s.session_id||s.title||"");
  const v=SUBAV[seed%SUBAV.length];
  const a=` width="${size}" height="${size}" alt="" aria-hidden="true" draggable="false">`;
  // Only a busy session animates — the seeded icon itself scale-pulses.
  const pulse=s.proc_state==="busy"?" pulse":"";
  return `<span class="subav-wrap${pulse}"><img class="subav subav-l" src="${v.l}"${a}<img class="subav subav-d" src="${v.d}"${a}</span>`;
}

/* ---------- minimal markdown ---------- */
function md(src){
  const parts=String(src).split(/(```[\s\S]*?(?:```|$))/);
  let out="";
  for(let i=0;i<parts.length;i++){
    let p=parts[i];
    if(i%2===1){ // fenced code
      const m=p.match(/^```(\w*)\n?([\s\S]*?)(?:```)?$/);
      out+="<pre><code>"+esc(m?m[2]:p)+"</code></pre>";
      continue;
    }
    p=esc(p);
    p=p.replace(/`([^`\n]+)`/g,"<code>$1</code>");
    const lines=p.split("\n"), buf=[];
    let list=null;
    const flushList=()=>{if(list){buf.push(`<${list}>`+listItems.join("")+`</${list}>`);list=null;listItems=[]}};
    let listItems=[];
    for(const ln of lines){
      const h=ln.match(/^(#{1,4})\s+(.*)/);
      const ul=ln.match(/^\s*[-*•]\s+(.*)/);
      const ol=ln.match(/^\s*\d+[.)]\s+(.*)/);
      if(h){flushList();buf.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`)}
      else if(ul){if(list!=="ul"){flushList();list="ul"}listItems.push("<li>"+ul[1]+"</li>")}
      else if(ol){if(list!=="ol"){flushList();list="ol"}listItems.push("<li>"+ol[1]+"</li>")}
      else{flushList();buf.push(ln)}
    }
    flushList();
    p=buf.join("\n");
    p=p.replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>");
    p=p.replace(/(^|\n)\s*\n/g,"\n").split(/\n{2,}/).map(x=>x.trim()?(/^<h|^<pre|^<ul|^<ol/.test(x)?x:"<p>"+x.replace(/\n/g,"<br>")+"</p>"):"").join("");
    out+=p;
  }
  return out;
}

/* ---------- status / session helpers ---------- */
const ST_ICON={warning:"hourglass",success:"checkCircle",error:"xCircle",neutral:"dot"};
function latestTask(sid){
  // Chronological choice on valid created_at; rows without a parseable
  // timestamp sort as -Infinity (never outrank a dated row), and equal keys
  // resolve to the later array index — so all-legacy lists still return the
  // last appended task.
  let best=null,bestTs=-Infinity,bestIdx=-1;
  tasks.forEach((t,i)=>{
    if(t.session_id!==sid)return;
    const ts=Date.parse(t.created_at);
    const v=Number.isFinite(ts)?ts:-Infinity;
    if(v>bestTs||(v===bestTs&&i>bestIdx)){best=t;bestTs=v;bestIdx=i}
  });
  return best;
}
const PROC_STATUS=Object.freeze({
  busy:{label:"Running",tone:"running"},
  spawning:{label:"Starting",tone:"running"},
  ready:{label:"Ready",tone:"neutral"},
  idle_unloaded:{label:"Idle",tone:"neutral"},
});
function statusOf(s){
  const st=s.proc_state;
  if(st==="dead"){
    const t=latestTask(s.session_id);
    if(t&&t.status==="failed")return{label:"Failed",tone:"error"};
    return{label:"Done",tone:"success"};
  }
  const m=PROC_STATUS[st];
  return m?{label:m.label,tone:m.tone}:{label:st||"Unknown",tone:"neutral"};
}
function statusGlyph(tone,size=12){
  return tone==="running"?spin():icon(ST_ICON[tone]||"dot",size);
}
/* Task working duration: the clock starts strictly at a valid started_at —
   never created_at, queue time, or session age — and freezes at a valid
   finished_at for terminal statuses. Anything missing/invalid renders nothing;
   a finished_at earlier than started_at clamps to zero. */
const fmtDur=ms=>{
  if(!Number.isFinite(ms))return"";
  const s=Math.max(0,Math.floor(ms/1e3));
  if(s<60)return s+"s";
  const m=Math.floor(s/60),rs=s%60;
  if(m<60)return rs?m+"m "+rs+"s":m+"m";
  const h=Math.floor(m/60),rm=m%60;
  return rm?h+"h "+rm+"m":h+"h"};
function taskDur(t){
  if(!t)return null;
  const start=Date.parse(t.started_at);
  if(!Number.isFinite(start))return null;
  if(t.status==="queued"||t.status==="running")return{start,end:null};
  const end=Date.parse(t.finished_at);
  if(!Number.isFinite(end))return null;
  return{start,end};
}
function durText(d){
  const s=fmtDur((d.end===null?Date.now():d.end)-d.start);
  return s?" · "+s:"";
}
function durSpan(t,cls){
  const d=taskDur(t);if(!d)return"";
  return `<span class="${cls}" data-tid="${esc(t.task_id)}">${durText(d)}</span>`;
}

/* ---------- task token usage (latest task only) ----------
   Adapters report different shapes; normalize them here once:
   snake_case  {input_tokens, cached_input_tokens, output_tokens}   (codex)
   camelCase   {inputTokens, cachedReadTokens, outputTokens}        (ACP)
   Devin ACP   {_meta:{"cognition.ai/inputTokens", ...}}            (usage_update)
   Headline tokens = max(input - cached, 0) + output; reasoning output is
   already inside output and never added separately. When no input/output
   counters exist, fall back to a trustworthy total ("used" is a context-
   window occupancy snapshot — an overcount across turns, but the best
   honest number available). */
function usageNums(u){
  if(!u||typeof u!=="object")u={};
  const m=typeof u._meta==="object"&&u._meta?u._meta:{};
  const num=v=>Number.isFinite(v)&&v>=0?v:null;
  const pick=(...keys)=>{for(const k of keys)for(const src of[u,m]){
    const v=num(src[k]);if(v!==null)return v}return null};
  return{
    input:pick("input_tokens","inputTokens","cognition.ai/inputTokens"),
    cached:pick("cached_input_tokens","cachedInputTokens","cachedReadTokens","cognition.ai/cachedReadTokens"),
    output:pick("output_tokens","outputTokens","cognition.ai/outputTokens"),
    total:pick("total_tokens","totalTokens","total","used","cognition.ai/totalTokens"),
  };
}
function tokCount(u){
  const n=usageNums(u);
  if(n.input===null&&n.output===null)return n.total;
  return Math.max(n.input-(n.cached||0),0)+n.output;
}
/* Segmented lowercase, matching "1m100k" for 1,100,000. The thousands
   segment is not zero-padded and drops entirely when it rounds to zero:
   1,005,000 -> "1m5k", exactly 1,000,000 -> "1m". */
const fmtTok=n=>{
  if(!Number.isFinite(n)||n<0)return"";
  n=Math.floor(n);
  if(n<1000)return String(n);
  if(n<1e6)return Math.floor(n/1000)+"k";
  const m=Math.floor(n/1e6),k=Math.floor(n%1e6/1e3);
  return k?m+"m"+k+"k":m+"m"};
function tokSpan(t,cls){
  const s=t&&t.usage?fmtTok(tokCount(t.usage)):"";
  return s?`<span class="${cls}"> · ${s} tok</span>`:"";
}
function tickDurations(){
  document.querySelectorAll("[data-tid]").forEach(el=>{
    const d=taskDur(tasks.find(x=>x.task_id===el.dataset.tid));
    el.textContent=d?durText(d):"";
  });
}
const baseName=p=>(p||"").split(/[\\/]/).filter(Boolean).pop()||"";

/* ---------- sidebar ---------- */
let lastSidebarSig="";
function renderSidebar(){
  const el=$("#sesslist");
  const order={busy:0,spawning:1,ready:1,idle_unloaded:2,dead:3};
  const sorted=[...sessions].sort((a,b)=>(order[a.proc_state]??4)-(order[b.proc_state]??4)
    || new Date(b.last_active_at)-new Date(a.last_active_at));
  const sig=(selected||"")+"|"+sorted.map(s=>{
    const t=latestTask(s.session_id);
    return [s.session_id,s.proc_state,s.last_active_at,s.title,s.agent,s.cwd,
      t?t.task_id:"",t?t.status:"",t?t.message:"",t?t.started_at:"",
      t?t.finished_at:"",t?t.created_at:"",t?JSON.stringify(t.usage||0):""].join(" ");
  }).join("|");
  if(sig===lastSidebarSig)return;
  lastSidebarSig=sig;
  const focusId=document.activeElement&&document.activeElement.dataset
    ?document.activeElement.dataset.id:null;
  el.innerHTML=sorted.map(s=>{
    const st=statusOf(s);
    const t=latestTask(s.session_id);
    const sub=t&&t.message?t.message.split("\n")[0].slice(0,80):baseName(s.cwd);
    const sel=s.session_id===selected;
    return `<button type="button" role="option" class="sess ${sel?"sel":""}" data-id="${esc(s.session_id)}"
        aria-selected="${sel}"${sel?' aria-current="true"':""} title="${esc(s.title||s.session_id)}">
      <span class="sicon">${agentAvatar(s,18)}</span>
      <span class="smeta">
        <span class="stitle">${esc(s.title||s.session_id)}</span>
        <span class="sstatus"><span class="sgr glyph--${st.tone}">${statusGlyph(st.tone)}</span><span>${esc(st.label)}</span>${durSpan(t,"sdur")}${tokSpan(t,"stok")}</span>
        ${sub?`<span class="ssub">${esc(sub)}</span>`:""}
      </span>
    </button>`}).join("")||'<div class="empty" style="margin-top:40px">No sessions</div>';
  el.querySelectorAll(".sess").forEach(d=>d.onclick=()=>select(d.dataset.id));
  if(focusId){
    const f=[...el.querySelectorAll(".sess")].find(d=>d.dataset.id===focusId);
    if(f)f.focus();
  }
}
$("#sesslist").addEventListener("keydown",e=>{
  if(e.key!=="ArrowDown"&&e.key!=="ArrowUp")return;
  const rows=[...e.currentTarget.querySelectorAll(".sess")];
  const i=rows.indexOf(document.activeElement);
  if(i<0)return;
  e.preventDefault();
  rows[e.key==="ArrowDown"?Math.min(i+1,rows.length-1):Math.max(i-1,0)].focus();
});

/* ---------- session header ---------- */
function renderSessionHeader(){
  const el=$("#hwrap");
  const s=sessions.find(x=>x.session_id===selected);
  if(!s){el.innerHTML='<div class="hplaceholder">Select a session</div>';return}
  const st=statusOf(s);
  const t=latestTask(s.session_id);
  const repo=baseName(s.cwd);
  el.innerHTML=`<div class="avatar">${agentAvatar(s,26)}</div>
    <div class="hbody">
      <h2 class="htitle">${esc(s.title||s.session_id)}</h2>
      <div class="hsub">
        <span class="hstatus"><span class="sgr glyph--${st.tone}">${statusGlyph(st.tone,13)}</span> ${esc(st.label)}${durSpan(t,"hdur")}${tokSpan(t,"htok")}</span>
        <span class="badge">${esc(s.agent)}</span>
        ${s.model?`<span class="badge">${esc(s.model)}</span>`:""}
        ${repo?`<span class="hsep">|</span><span class="hrepo" title="${esc(s.cwd||"")}">Working repo · ${esc(repo)}</span>`:""}
      </div>
    </div>
    <div class="hmeta">${s.turns??0} turn${s.turns===1?"":"s"}${s.last_active_at?" · "+ago(s.last_active_at):""}</div>`;
}

/* ---------- conversation rendering ---------- */
const content=$("#content");
let curMsg=null, curThink=null, curToolGroup=null, tools={}; // open blocks for streaming merge
let lastPromptTs=null;      // ts of the last prompt rendered — pairs legacy turn_ends
const dirty=new Set();      // stream blocks whose text changed since last flush
const panes={};             // id -> stashed {holder,curMsg,curThink,curToolGroup,tools}
const paneLru=[],cacheLru=[];
const MAX_PANES=5,MAX_CACHE=10;
let replaying=null;             // session id whose replay is painting the live pane
const replayGen={};             // id -> replay generation token (abort stamp, per session)
const rendered={};          // id -> count of eventsCache[id] entries already in the DOM

function flushBlocks(){
  for(const b of dirty){
    if(b.tagName==="DETAILS"){
      b.querySelector(".body").textContent=b._text;
      b.querySelector(".tlabel").textContent=
        `Thinking · ${b._text.split(/\s+/).filter(Boolean).length} words`;
    }else b.querySelector(".msg-body").innerHTML=md(b._text);
  }
  dirty.clear();
}
function closeBlocks(){flushBlocks();curMsg=null;curThink=null;curToolGroup=null}
function toolGroup(){
  if(!curToolGroup){curToolGroup=document.createElement("div");
    curToolGroup.className="block toolgroup";content.appendChild(curToolGroup)}
  return curToolGroup;
}
function addPrompt(e){
  closeBlocks();
  lastPromptTs=e.ts;
  const user=e.src==="dashboard";
  const d=document.createElement("div");d.className="block card prompt"+(user?" user":"");
  const long=e.text.length>900;
  d.innerHTML=`<div class="chead">${icon(user?"msg":"doc",15)}<span>${user?"User Message":"Dispatched Message"}</span><span class="ctime">${fmtTs(e.ts)}</span></div>
    <pre class="ptext ${long?"clamp":""}">${esc(e.text)}</pre>`;
  if(long){const x=document.createElement("button");x.type="button";x.className="expand";x.textContent="show more";
    x.onclick=()=>{d.querySelector("pre").classList.remove("clamp");x.remove()};d.appendChild(x)}
  content.appendChild(d);
}
function msgBlock(ts){
  if(!curMsg){curMsg=document.createElement("div");curMsg.className="block card msg";
    curMsg.innerHTML=`<div class="chead">${icon("msg",15)}<span>Agent</span><span class="ctime">${fmtTs(ts)}</span></div><div class="msg-body"></div>`;
    content.appendChild(curMsg);curMsg._text=""}
  curThink=null;curToolGroup=null;
  return curMsg;
}
function addMsg(e){
  const b=msgBlock(e.ts);b._text+=e.text;dirty.add(b);
}
function addThink(e){
  if(!curThink){curThink=document.createElement("details");curThink.className="block think";
    curThink.innerHTML=`<summary>${icon("sparkle",14)}<span class="tlabel">Thinking</span><span class="chev">${icon("chevron",13)}</span></summary><div class="body"></div>`;
    content.appendChild(curThink);curThink._text=""}
  curMsg=null;curToolGroup=null;
  curThink._text+=e.text;dirty.add(curThink);
}
const TOOL_ICON={execute:"monitor",read:"doc",search:"search",edit:"edit",fetch:"external"};
function addTool(e){
  flushBlocks();
  const g=toolGroup();curMsg=null;curThink=null;
  const kind=String(e.kind||"tool").toLowerCase();
  const row=document.createElement("div");row.className="tool";
  row.innerHTML=`<span class="ticon">${icon(TOOL_ICON[kind]||"gear",15)}</span>
    <span class="tmain"><span class="tcap">${esc(kind.charAt(0).toUpperCase()+kind.slice(1))}</span>
      <span class="ttitle" title="${esc(e.input||"")}">${esc(e.title||e.id)}</span></span>
    <span class="tright"><span class="dur"></span><span class="st in_progress">${spin()}</span></span>`;
  g.appendChild(row);
  const rec={el:row,start:e.ts,status:"in_progress"};
  tools[e.id]=rec;
  if(e.input){const det=document.createElement("details");det.className="tooldetail";
    det.innerHTML=`<summary><span class="chev">${icon("chevron",11)}</span>input</summary><pre>${esc(e.input)}</pre>`;
    row.after(det);
    row.style.cursor="pointer";row.onclick=()=>det.open=!det.open;}
}
function addToolStatus(e){
  const r=tools[e.id];if(!r)return;
  r.status=e.status;
  const st=r.el.querySelector(".st");
  st.className="st "+e.status;
  st.innerHTML=e.status==="in_progress"?spin()
    :e.status==="completed"?icon("checkCircle",15)
    :(e.status==="failed"||e.status==="error")?icon("xCircle",15)
    :esc(e.status);
  if(e.status!=="in_progress"&&r.start){
    const s=(new Date(e.ts)-new Date(r.start))/1000;
    r.el.querySelector(".dur").textContent=s>=1?s.toFixed(1)+"s":Math.round(s*1000)+"ms";
  }
}
/* Turn duration: join on the task row via task_id (started_at→finished_at is
   the true task duration and stays frozen once terminal). A still-running
   task ends the interval at the turn event's own timestamp — an unparseable
   one yields no duration rather than an invented now-based value. Legacy
   transcripts carry no task_id, so fall back to the elapsed time since the
   preceding prompt event. */
function turnDurMs(e,promptTs){
  const tk=e.task?tasks.find(x=>x.task_id===e.task):null;
  const td=tk?taskDur(tk):null;
  if(td){
    const end=td.end===null?Date.parse(e.ts):td.end;
    return Number.isFinite(end)?end-td.start:null;
  }
  const a=Date.parse(e.ts),b=Date.parse(promptTs);
  return Number.isFinite(a)&&Number.isFinite(b)?a-b:null;
}
function addTurn(e){
  closeBlocks();
  const d=document.createElement("div");d.className="turnend";
  const ms=turnDurMs(e,lastPromptTs);
  // Visible text: "turn ended · <duration>" — plus the stop reason only when
  // it is not end_turn. The wall-clock timestamp (and any error detail) live
  // in the tooltip, not the text.
  const parts=["turn ended"];
  const dur=ms===null?"":fmtDur(ms);
  if(dur)parts.push(dur);
  if(e.stop_reason&&e.stop_reason!=="end_turn")parts.push(e.stop_reason);
  const tip=[fmtTs(e.ts),e.error].filter(Boolean).join(" · ");
  if(tip)d.title=tip;
  d.innerHTML=`${icon("dot",10)}<span>${esc(parts.join(" · "))}</span>`;
  content.appendChild(d);
}
function addErr(e){
  closeBlocks();
  const d=document.createElement("div");d.className="turnend err";
  d.innerHTML=`${icon("xCircle",10)}<span>${esc(e.text||"error")} · ${fmtTs(e.ts)}</span>`;
  content.appendChild(d);
}
const handlers={prompt:addPrompt,msg:addMsg,think:addThink,tool:addTool,
  tool_status:addToolStatus,turn:addTurn,error:addErr};

function applyEvents(evs){
  const nearBottom=content.scrollHeight-content.scrollTop-content.clientHeight<120;
  for(const e of evs)(handlers[e.t]||(()=>{}))(e);
  flushBlocks();
  if(nearBottom)content.scrollTop=content.scrollHeight;
  $("#backtop").style.display=nearBottom?"none":"flex";
}

/* Chunked cold/backlog replay: render the cached event stream in short
   requestAnimationFrame slices so switching stays responsive. Aborts via the
   replayGen token when the user switches away or the transcript resets. */
function startReplay(id,pin){
  const gen=(replayGen[id]||0)+1;
  replayGen[id]=gen;
  replaying=id;
  replay(id,gen,pin).finally(()=>{if(gen===replayGen[id]&&replaying===id)replaying=null});
}
async function replay(id,gen,pin){
  const ph=content.querySelector(".empty");if(ph)ph.remove();
  let i=rendered[id]||0;
  while(true){
    const evs=eventsCache[id];
    if(!evs||i>=evs.length)return;
    if(gen!==replayGen[id]||id!==selected)return;
    const nb=pin||content.scrollHeight-content.scrollTop-content.clientHeight<120;
    const t0=performance.now();
    while(i<evs.length&&performance.now()-t0<8)(handlers[evs[i].t]||(()=>{}))(evs[i++]);
    rendered[id]=i;
    flushBlocks();
    if(nb)content.scrollTop=content.scrollHeight;
    $("#backtop").style.display=nb?"none":"flex";
    await new Promise(r=>requestAnimationFrame(r));
  }
}

/* Per-session pane stash: warm re-selection moves the saved DOM nodes back
   (folds, handlers and merge state survive) instead of replaying events. */
function dropPane(id){
  delete panes[id];
  const i=paneLru.indexOf(id);if(i>=0)paneLru.splice(i,1);
  delete rendered[id];
}
function stashPane(id){
  flushBlocks();
  const holder=document.createElement("div");
  while(content.firstChild)holder.appendChild(content.firstChild);
  panes[id]={holder,curMsg,curThink,curToolGroup,tools,lastPromptTs};
  curMsg=curThink=curToolGroup=null;tools={};lastPromptTs=null;
  const i=paneLru.indexOf(id);if(i>=0)paneLru.splice(i,1);
  paneLru.push(id);
  while(paneLru.length>MAX_PANES)dropPane(paneLru[0]);
}
function touchCache(id){
  const i=cacheLru.indexOf(id);if(i>=0)cacheLru.splice(i,1);
  cacheLru.push(id);
  while(cacheLru.length>MAX_CACHE){
    const k=cacheLru.findIndex(x=>x!==selected&&x!==replaying);
    if(k<0)break;
    dropCache(cacheLru.splice(k,1)[0]);
  }
}
function dropCache(id){
  delete eventsCache[id];delete offsets[id];delete rendered[id];delete replayGen[id];
  const i=cacheLru.indexOf(id);if(i>=0)cacheLru.splice(i,1);
  dropPane(id);
}

async function pollEvents(){
  const id=selected;
  if(!id)return;
  const off=offsets[id]||0;
  try{
    const r=await fetch(`/api/events?session=${id}&offset=${off}`);
    if(!r.ok)return;
    const j=await r.json();
    if(j.reset)eventsCache[id]=[];
    eventsCache[id]=(eventsCache[id]||[]).concat(j.events);
    offsets[id]=j.offset;
    touchCache(id);
    if(j.reset){                      // transcript rotated: that session's pane/replay
      dropPane(id);                   // are invalid — other sessions' replays are not
      replayGen[id]=(replayGen[id]||0)+1;
      if(replaying===id)replaying=null;
    }
    if(id!==selected)return;          // user switched away mid-flight: keep cache, skip DOM
    if(j.reset){
      content.innerHTML="";closeBlocks();tools={};lastPromptTs=null;
      startReplay(id,true);
      return;
    }
    if(replaying===id)return;         // the replay loop consumes the shared cache itself
    const ph=content.querySelector(".empty");if(ph)ph.remove();
    const pend=eventsCache[id].length-(rendered[id]||0);
    if(pend>0){
      if((rendered[id]||0)===0||pend>300)startReplay(id,(rendered[id]||0)===0);
      else{
        applyEvents(eventsCache[id].slice(-pend));
        rendered[id]=eventsCache[id].length;
      }
    }
    else if(!content.children.length)content.innerHTML='<div class="empty">No events</div>';
  }catch(e){}
}

function setLive(on){
  const l=$("#live");l.className=on?"on":"off";
  l.querySelector(".lt").textContent=on?"live":"disconnected";
}

async function pollOverview(){
  try{
    const r=await fetch("/api/overview");if(!r.ok)return;
    const j=await r.json();sessions=j.sessions;tasks=j.tasks;
    const known=new Set(sessions.map(s=>s.session_id));
    for(const id of new Set([...Object.keys(eventsCache),...Object.keys(panes),
        ...Object.keys(offsets),...Object.keys(rendered)]))
      if(!known.has(id)&&id!==selected)dropCache(id);
    renderSidebar();renderSessionHeader();
    setLive(true);
    if(!selected&&sessions.length)select(sessions.find(s=>s.proc_state==="busy")?.session_id||sessions[0].session_id);
  }catch(e){setLive(false)}
}

/* ---------- chat bar ----------
   A sent message lives in the bridge outbox until the target session can
   accept it (busy sessions requeue it, subject to a 24h wall-clock expiry).
   The poll therefore has no fixed cap: it runs while the page is alive and
   reports the queue state the bridge recorded — never "sent" for a mere
   outbox enqueue. Acceptance by dispatch_task is reported as "dispatched ·
   <task_id>" — it is not called delivery: the adapter may still fail
   afterwards. Per-session status survives session switches. */
const sendState={};  // session_id -> {name, text, final}
const sendPolls={};  // outbox name -> polling loop already running
function setChatStatus(s){$("#chatstatus").textContent=s||""}
function sendText(j){
  const st=j&&j.state;
  return st==="waiting_busy"?"waiting for agent — session busy…"
    :st==="waiting_owner"?"waiting for agent's bridge…"
    :st==="delivering"?"delivering…"
    :"queued…";
}
function setSendState(sid,text,final){
  sendState[sid]=Object.assign(sendState[sid]||{},{text,final:!!final});
  if(selected===sid)setChatStatus(text);
}
async function sendChat(){
  const ta=$("#chatinput"),text=ta.value.trim();
  if(!text||!selected)return;
  const sid=selected;
  ta.value="";ta.style.height="";setSendState(sid,"sending…");
  try{
    const r=await fetch("/api/send",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:sid,text})});
    const j=await r.json();
    if(!j.ok){setSendState(sid,j.error||"send failed",true);return}
    setSendState(sid,"queued…");
    sendState[sid].name=j.name;
    pollSendStatus(j.name,sid);
  }catch(e){setSendState(sid,"send failed",true)}
}
async function pollSendStatus(name,sid){
  if(sendPolls[name])return;
  sendPolls[name]=true;
  const mine=()=>sendState[sid]&&sendState[sid].name===name;
  try{
    while(true){
      await new Promise(r=>setTimeout(r,2000));
      let j;
      try{
        const r=await fetch(`/api/send_status?name=${encodeURIComponent(name)}`);
        j=await r.json();
      }catch(e){continue}              // network blip: keep waiting
      if(!mine())return;              // a newer send to this session owns the status line
      if(j&&j.pending){setSendState(sid,sendText(j));continue}
      const done=j||{};
      setSendState(sid,
        done.ok?`dispatched${done.task_id?" · "+done.task_id:""}`
          :(done.error||"send failed"),true);
      return;
    }
  }finally{delete sendPolls[name]}
}
const chatInput=$("#chatinput");
chatInput.addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendChat()}});
chatInput.addEventListener("input",()=>{
  chatInput.style.height="auto";
  chatInput.style.height=Math.min(chatInput.scrollHeight,160)+"px"});
$("#chatsend").onclick=sendChat;

/* ---------- narrow-screen sidebar drawer ---------- */
const sidebar=$("#sidebar"),backdrop=$("#backdrop"),menubtn=$("#menubtn");
function closeSidebar(){sidebar.classList.remove("open");backdrop.classList.remove("open");
  menubtn.setAttribute("aria-expanded","false")}
menubtn.onclick=()=>{const open=!sidebar.classList.contains("open");
  sidebar.classList.toggle("open",open);backdrop.classList.toggle("open",open);
  menubtn.setAttribute("aria-expanded",String(open))};
backdrop.onclick=closeSidebar;
addEventListener("keydown",e=>{if(e.key==="Escape")closeSidebar()});

/* ---------- theme preference (System · Light · Dark) ---------- */
const THEME_KEY="ab-theme";
const mq=matchMedia("(prefers-color-scheme: dark)");
const THEME_ICONS={system:"monitor",light:"sun",dark:"moon"};
const themeBtns=[...document.querySelectorAll(".theme-options [data-theme-pref]")];
function themePref(){
  try{const p=localStorage.getItem(THEME_KEY);
    return p==="system"||p==="light"||p==="dark"?p:"system"}catch(e){return"system"}
}
function applyTheme(){
  const p=themePref();
  document.documentElement.setAttribute("data-theme",
    p==="system"?(mq.matches?"dark":"light"):p);
  for(const b of themeBtns){
    const on=b.dataset.themePref===p;
    b.setAttribute("aria-checked",String(on));
    b.tabIndex=on?0:-1;
  }
}
function setThemePref(p){
  try{localStorage.setItem(THEME_KEY,p)}catch(e){}
  applyTheme();
}
themeBtns.forEach((b,i)=>{
  b.insertAdjacentHTML("afterbegin",icon(THEME_ICONS[b.dataset.themePref],13));
  b.addEventListener("click",()=>setThemePref(b.dataset.themePref));
  b.addEventListener("keydown",e=>{
    if(e.key!=="ArrowRight"&&e.key!=="ArrowDown"&&e.key!=="ArrowLeft"&&e.key!=="ArrowUp")return;
    e.preventDefault();
    const d=(e.key==="ArrowRight"||e.key==="ArrowDown")?1:-1;
    const n=themeBtns[(i+d+themeBtns.length)%themeBtns.length];
    n.focus();setThemePref(n.dataset.themePref);
  });
});
mq.addEventListener("change",()=>{if(themePref()==="system")applyTheme()});
addEventListener("storage",e=>{if(e.key===THEME_KEY)applyTheme()});
applyTheme();

function select(id){
  const s=sessions.find(x=>x.session_id===id);
  if(selected!==id){
    const prev=selected,wasReplaying=replaying;
    selected=id;
    if(prev)replayGen[prev]=(replayGen[prev]||0)+1;   // abort the outgoing replay only
    replaying=null;
    chatInput.disabled=false;$("#chatsend").disabled=false;
    setChatStatus(sendState[id]?sendState[id].text:"");
    chatInput.placeholder=s?`Send an instruction to ${s.title||s.session_id}…`:"Send an instruction…";
    if(prev){
      const done=(rendered[prev]||0)===(eventsCache[prev]||[]).length;
      if(wasReplaying===prev&&!done){ // mid-replay partial pane: discard, re-render on return
        content.innerHTML="";closeBlocks();tools={};dropPane(prev);
      }else stashPane(prev);
    }
    const p=panes[id];
    if(p){
      delete panes[id];
      const i=paneLru.indexOf(id);if(i>=0)paneLru.splice(i,1);
      while(p.holder.firstChild)content.appendChild(p.holder.firstChild);
      curMsg=p.curMsg;curThink=p.curThink;curToolGroup=p.curToolGroup;tools=p.tools;
      lastPromptTs=p.lastPromptTs||null;
      content.scrollTop=content.scrollHeight;
      $("#backtop").style.display="none";
    }else{
      closeBlocks();tools={};lastPromptTs=null;rendered[id]=0;
      if(eventsCache[id]&&eventsCache[id].length){
        content.innerHTML="";
        startReplay(id,true);
      }else{
        eventsCache[id]=[];offsets[id]=0;
        content.innerHTML='<div class="empty">Loading…</div>';
      }
    }
    touchCache(id);
    pollEvents();
  }
  renderSidebar();renderSessionHeader();closeSidebar();
}

$("#backtop").onclick=()=>{content.scrollTop=content.scrollHeight};
content.addEventListener("scroll",()=>{
  const nb=content.scrollHeight-content.scrollTop-content.clientHeight<120;
  $("#backtop").style.display=nb?"none":"flex";
});

pollOverview();
ovTimer=setInterval(pollOverview,3000);
pollTimer=setInterval(pollEvents,1500);
setInterval(tickDurations,1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/overview":
            state = load_state()
            self._json(
                {
                    "sessions": state.get("sessions", []),
                    "tasks": [
                        {
                            k: t.get(k)
                            for k in (
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
                                "created_at",
                                "started_at",
                                "finished_at",
                            )
                        }
                        for t in state.get("tasks", [])
                    ],
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
                self._json({"error": "bad session"}, 400)
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
            if not re.match(r"^msg_\d+_[0-9a-f]{8}\.json$", name):
                self._json({"error": "bad name"}, 400)
                return
            done = OUTBOX_DIR / "done" / name
            if done.exists():
                try:
                    self._json(json.loads(done.read_text(encoding="utf-8", errors="replace")))
                finally:
                    done.unlink(missing_ok=True)
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
                },
                404,
            )
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/send":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "bad request"}, 400)
                return
            session = str(payload.get("session") or "")
            text = str(payload.get("text") or "").strip()
            if not SAFE_ID.match(session):
                self._json({"ok": False, "error": "bad session"}, 400)
                return
            if not text or len(text) > 20000:
                self._json({"ok": False, "error": "empty or too long"}, 400)
                return
            state = load_state()
            known = {s.get("session_id") for s in state.get("sessions", [])}
            if session not in known:
                self._json({"ok": False, "error": "unknown session"}, 404)
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
        # navigator.sendBeacon uses POST
        if u.path == "/api/presence":
            q = parse_qs(u.query)
            client_id = (q.get("id") or [""])[0]
            bye = (q.get("bye") or ["0"])[0] in ("1", "true")
            self._json({"ok": True, "clients": presence_update(client_id, bye)})
            return
        self._json({"error": "not found"}, 404)


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
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Agent Bridge dashboard → http://127.0.0.1:{args.port}")
    print(f"data dir: {BRIDGE_DIR}")
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
