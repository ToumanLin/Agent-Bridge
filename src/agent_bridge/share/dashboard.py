#!/usr/bin/env python3
"""Agent Bridge dashboard — view MCP bridge sessions and their conversations.

Reads state.json and transcripts/*.jsonl from the Agent Bridge data directory
and serves a live-updating web UI.

Usage:
    python dashboard.py [--port 8787] [--dir <bridge-data-dir>]

Then open http://127.0.0.1:8787
"""

import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BRIDGE_DIR = Path(os.environ.get("BRIDGE_DIR", Path(__file__).resolve().parent))
STATE_FILE = BRIDGE_DIR / "state.json"
TRANSCRIPT_DIR = BRIDGE_DIR / "transcripts"

# Open-tab presence: browser heartbeats via /api/presence; the bridge checks
# /api/client_state to decide whether to pop the dashboard up.
PRESENCE = {}  # client_id -> last-seen monotonic timestamp
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
        return {"t": "prompt", "ts": ts, "text": d.get("text", "")}
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
        return {"t": "turn", "ts": ts, "stop_reason": d.get("stop_reason")}
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
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2d333f;
  --text:#e6edf3; --dim:#8b949e; --accent:#58a6ff; --green:#3fb950;
  --yellow:#d29922; --red:#f85149; --purple:#bc8cff; --cyan:#39c5cf;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--panel);
  border-bottom:1px solid var(--border)}
header h1{font-size:15px;margin:0;font-weight:600}
header .sub{color:var(--dim);font-size:12px}
header .spacer{flex:1}
#live{font-size:12px;color:var(--dim)}
#live.on{color:var(--green)}
main{flex:1;display:flex;min-height:0}
#sidebar{width:340px;min-width:280px;border-right:1px solid var(--border);overflow-y:auto;
  background:var(--panel)}
#content{flex:1;overflow-y:auto;padding:16px 24px}
.sess{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer}
.sess:hover{background:var(--panel2)}
.sess.sel{background:var(--panel2);border-left:3px solid var(--accent);padding-left:11px}
.sess .title{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.sess .meta{display:flex;gap:8px;align-items:center;margin-top:4px;font-size:11px;color:var(--dim);flex-wrap:wrap}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:10px;font-weight:600;
  background:#333;border:1px solid var(--border);color:var(--dim)}
.badge.busy{background:#1a3a22;color:var(--green);border-color:#2ea04344}
.badge.idle_unloaded{background:#2a2a14;color:var(--yellow);border-color:#9e6a0333}
.badge.dead{background:#2d1a1a;color:var(--red);border-color:#f8514933}
.badge.running{background:#1a3a22;color:var(--green)}
.badge.completed{background:#16304a;color:var(--accent)}
.badge.failed,.badge.error{background:#3d1414;color:var(--red)}
.badge.cancelled{background:#2a2a14;color:var(--yellow)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.dot.busy{background:var(--green);animation:pulse 1.4s infinite}
.dot.idle_unloaded{background:var(--yellow)}
.dot.dead{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.taskline{font-size:11px;color:var(--dim);margin-top:3px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
/* conversation blocks */
.block{margin:0 auto 14px;max-width:900px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px}
.card .who{font-size:11px;color:var(--dim);margin-bottom:6px;display:flex;gap:8px;align-items:center}
.card.prompt{border-left:3px solid var(--purple)}
.card.prompt .who{color:var(--purple)}
.card pre{white-space:pre-wrap;word-break:break-word;margin:0;font-family:inherit;font-size:13px}
.msg-body{font-size:14px}
.msg-body pre{background:#0a0e14;border:1px solid var(--border);border-radius:6px;
  padding:10px;overflow-x:auto;font-family:Consolas,Menlo,monospace;font-size:12.5px}
.msg-body code{font-family:Consolas,Menlo,monospace;background:#21262d;border-radius:4px;
  padding:1px 5px;font-size:12.5px}
.msg-body pre code{background:none;padding:0}
.msg-body h1,.msg-body h2,.msg-body h3,.msg-body h4{margin:12px 0 6px;font-size:15px}
.msg-body ul,.msg-body ol{margin:6px 0;padding-left:22px}
.msg-body p{margin:6px 0}
details.think{background:var(--panel);border:1px dashed var(--border);border-radius:8px;padding:6px 12px}
details.think summary{cursor:pointer;color:var(--cyan);font-size:12px;list-style:none}
details.think summary::before{content:"💭 ";}
details.think .body{color:var(--dim);font-size:12.5px;white-space:pre-wrap;margin-top:6px}
.toolgroup{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:4px 0}
.tool{display:flex;align-items:center;gap:8px;padding:4px 12px;font-size:12.5px}
.tool+.tool{border-top:1px solid #21262d}
.tool .kind{flex:none;font-size:10px;font-weight:700;padding:0 6px;border-radius:4px;
  text-transform:uppercase;letter-spacing:.4px}
.kind.execute{background:#16304a;color:var(--accent)}
.kind.read{background:#1a3a22;color:var(--green)}
.kind.search{background:#3d2a14;color:var(--yellow)}
.kind.edit{background:#2d1a3d;color:var(--purple)}
.kind.tool{background:#262c36;color:var(--dim)}
.tool .ttitle{flex:1;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool .dur{color:var(--dim);font-size:11px;flex:none}
.tool .st{flex:none;font-size:11px}
.st.in_progress{color:var(--yellow)}
.st.completed{color:var(--green)}
.st.failed,.st.error{color:var(--red)}
details.tooldetail{padding:0 12px 6px}
details.tooldetail summary{cursor:pointer;color:var(--dim);font-size:11px}
details.tooldetail pre{background:#0a0e14;border-radius:6px;padding:8px;font-size:11.5px;
  overflow-x:auto;white-space:pre-wrap;word-break:break-all;color:var(--dim);max-height:240px;overflow-y:auto}
.turnend{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:11px;margin:18px auto;max-width:900px}
.turnend::before,.turnend::after{content:"";flex:1;height:1px;background:var(--border)}
.empty{color:var(--dim);text-align:center;margin-top:80px;font-size:14px}
#backtop{position:fixed;right:24px;bottom:24px;background:var(--panel2);border:1px solid var(--border);
  color:var(--dim);border-radius:20px;padding:6px 14px;cursor:pointer;font-size:12px;display:none}
.clamp{max-height:120px;overflow:hidden;position:relative}
.clamp::after{content:"";position:absolute;bottom:0;left:0;right:0;height:40px;
  background:linear-gradient(transparent,var(--panel))}
.expand{cursor:pointer;color:var(--accent);font-size:11px;margin-top:4px;display:inline-block}
</style>
</head>
<body>
<header>
  <h1>Agent Bridge</h1><span class="sub">session dashboard</span>
  <span class="spacer"></span>
  <span id="live">connecting…</span>
</header>
<main>
  <div id="sidebar"></div>
  <div id="content"><div class="empty">Select a session</div></div>
</main>
<div id="backtop">↓ latest</div>
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

/* ---------- sidebar ---------- */
function renderSidebar(){
  const el=$("#sidebar");
  const order={busy:0,idle_unloaded:1};
  const sorted=[...sessions].sort((a,b)=>(order[a.proc_state]??2)-(order[b.proc_state]??2)
    || new Date(b.last_active_at)-new Date(a.last_active_at));
  el.innerHTML=sorted.map(s=>{
    const ts=tasks.filter(t=>t.session_id===s.session_id);
    const tline=ts.map(t=>`<div class="taskline"><span class="badge ${t.status}">${t.status}</span> ${esc(t.message.slice(0,80))}</div>`).join("");
    return `<div class="sess ${s.session_id===selected?'sel':''}" data-id="${s.session_id}">
      <div class="title">${esc(s.title||s.session_id)}</div>
      <div class="meta">
        <span><span class="dot ${s.proc_state}"></span>${s.proc_state}</span>
        <span class="badge">${esc(s.agent)}</span>
        ${s.model?`<span class="badge">${esc(s.model)}</span>`:""}
        <span>${s.turns} turn${s.turns===1?"":"s"}</span>
        <span>${ago(s.last_active_at)}</span>
      </div>
      <div class="meta" style="margin-top:2px"><span title="${esc(s.cwd)}">${esc((s.cwd||"").split(/[\\/]/).pop())}</span></div>
      ${tline}
    </div>`}).join("")||'<div class="empty" style="margin-top:40px">No sessions</div>';
  el.querySelectorAll(".sess").forEach(d=>d.onclick=()=>select(d.dataset.id));
}

/* ---------- conversation rendering ---------- */
const content=$("#content");
let curMsg=null, curThink=null, curToolGroup=null; // open blocks for streaming merge
const tools={}; // id -> {el,titleEl,stEl,startTs,status}

function closeBlocks(){curMsg=null;curThink=null;curToolGroup=null}
function toolGroup(){
  if(!curToolGroup){curToolGroup=document.createElement("div");
    curToolGroup.className="block toolgroup";content.appendChild(curToolGroup)}
  return curToolGroup;
}
function addPrompt(e){
  closeBlocks();
  const d=document.createElement("div");d.className="block card prompt";
  const long=e.text.length>900;
  d.innerHTML=`<div class="who"><span>Dispatch → worker</span><span>${fmtTs(e.ts)}</span></div>
    <pre class="${long?'clamp':''}">${esc(e.text)}</pre>`;
  if(long){const x=document.createElement("span");x.className="expand";x.textContent="show more";
    x.onclick=()=>{d.querySelector("pre").classList.remove("clamp");x.remove()};d.appendChild(x)}
  content.appendChild(d);
}
function msgBlock(){
  if(!curMsg){curMsg=document.createElement("div");curMsg.className="block card";
    curMsg.innerHTML='<div class="who"><span>Agent</span></div><div class="msg-body"></div>';
    content.appendChild(curMsg);curMsg._text=""}
  curThink=null;curToolGroup=null;
  return curMsg;
}
function addMsg(e){
  const b=msgBlock();b._text+=e.text;
  b.querySelector(".msg-body").innerHTML=md(b._text);
}
function addThink(e){
  if(!curThink){curThink=document.createElement("details");curThink.className="block think";
    curThink.innerHTML="<summary>Thinking…</summary><div class='body'></div>";
    content.appendChild(curThink);curThink._text=""}
  curMsg=null;curToolGroup=null;
  curThink._text+=e.text;
  curThink.querySelector(".body").textContent=curThink._text;
  const w=curThink._text.split(/\s+/).length;
  curThink.querySelector("summary").textContent=`Thinking · ${w} words`;
}
function addTool(e){
  const g=toolGroup();curMsg=null;curThink=null;
  const row=document.createElement("div");row.className="tool";
  row.innerHTML=`<span class="kind ${esc(e.kind)}">${esc(e.kind)}</span>
    <span class="ttitle" title="${esc(e.input||"")}">${esc(e.title||e.id)}</span>
    <span class="dur"></span><span class="st in_progress">…</span>`;
  g.appendChild(row);
  const rec={el:row,start:e.ts,status:"in_progress"};
  tools[e.id]=rec;
  if(e.input){const det=document.createElement("details");det.className="tooldetail";
    det.innerHTML=`<summary>input</summary><pre>${esc(e.input)}</pre>`;row.after(det);
    row.style.cursor="pointer";row.onclick=()=>det.open=!det.open;}
}
function addToolStatus(e){
  const r=tools[e.id];if(!r)return;
  r.status=e.status;
  const st=r.el.querySelector(".st");
  st.className="st "+e.status;
  st.textContent=e.status==="completed"?"✓":e.status==="in_progress"?"…":e.status;
  if(e.status!=="in_progress"&&r.start){
    const s=(new Date(e.ts)-new Date(r.start))/1000;
    r.el.querySelector(".dur").textContent=s>=1?s.toFixed(1)+"s":Math.round(s*1000)+"ms";
  }
}
function addTurn(e){
  closeBlocks();
  const d=document.createElement("div");d.className="turnend";
  d.textContent=`turn ended · ${e.stop_reason||"unknown"} · ${fmtTs(e.ts)}`;
  content.appendChild(d);
}
const handlers={prompt:addPrompt,msg:addMsg,think:addThink,tool:addTool,
  tool_status:addToolStatus,turn:addTurn};

function applyEvents(evs){
  const nearBottom=content.scrollHeight-content.scrollTop-content.clientHeight<120;
  for(const e of evs)(handlers[e.t]||(()=>{}))(e);
  if(nearBottom)content.scrollTop=content.scrollHeight;
  $("#backtop").style.display=nearBottom?"none":"block";
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
    if(id!==selected)return;          // user switched away mid-flight: keep cache, skip DOM
    if(j.reset){content.innerHTML="";closeBlocks();for(const k in tools)delete tools[k];}
    const ph=content.querySelector(".empty");if(ph)ph.remove();
    if(j.events.length)applyEvents(j.events);
    else if(!content.children.length)content.innerHTML='<div class="empty">No events</div>';
  }catch(e){}
}

async function pollOverview(){
  try{
    const r=await fetch("/api/overview");if(!r.ok)return;
    const j=await r.json();sessions=j.sessions;tasks=j.tasks;
    renderSidebar();
    $("#live").textContent="live";$("#live").className="on";
    if(!selected&&sessions.length)select(sessions.find(s=>s.proc_state==="busy")?.session_id||sessions[0].session_id);
  }catch(e){$("#live").textContent="disconnected";$("#live").className=""}
}

function select(id){
  if(selected===id)return;
  selected=id;renderSidebar();
  closeBlocks();for(const k in tools)delete tools[k];
  const cached=eventsCache[id];
  if(cached&&cached.length){
    content.innerHTML="";
    applyEvents(cached);
    content.scrollTop=content.scrollHeight;
  }else{
    eventsCache[id]=[];offsets[id]=0;
    content.innerHTML='<div class="empty">Loading…</div>';
  }
  pollEvents();
}

$("#backtop").onclick=()=>{content.scrollTop=content.scrollHeight};
content.addEventListener("scroll",()=>{
  const nb=content.scrollHeight-content.scrollTop-content.clientHeight<120;
  $("#backtop").style.display=nb?"none":"block";
});

pollOverview();
ovTimer=setInterval(pollOverview,3000);
pollTimer=setInterval(pollEvents,1500);
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
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        # navigator.sendBeacon uses POST
        u = urlparse(self.path)
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
    global BRIDGE_DIR, STATE_FILE, TRANSCRIPT_DIR
    if args.dir:
        BRIDGE_DIR = Path(args.dir).resolve()
        STATE_FILE = BRIDGE_DIR / "state.json"
        TRANSCRIPT_DIR = BRIDGE_DIR / "transcripts"
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Agent Bridge dashboard → http://127.0.0.1:{args.port}")
    print(f"data dir: {BRIDGE_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
