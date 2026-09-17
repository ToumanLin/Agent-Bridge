"""The dashboard single-page frontend document (HTML + CSS + JS).

This is the presentation domain of ``share/dashboard.py``: the entire
SPA is one raw string served verbatim at ``GET /``. Keeping it inline
preserves the strict CSP (``default-src 'none'; script-src
``'unsafe-inline'; style-src 'unsafe-inline'``), makes every response
byte-identical to the running backend, and sidesteps static-file
routing, cache invalidation, and wheel package-data config (Hatchling
ships this module automatically as part of ``src/agent_bridge``).

``dashboard.py`` re-exports ``PAGE`` so its import surface is
unchanged -- ``agent_bridge.share.dashboard.PAGE`` is this same object.
The Node harnesses (``tests/dashboard_status_behavior.js`` and
``scripts/bench_dashboard_replay.js``) read this file directly and
regex-match the raw-string assignment, so ``PAGE`` must remain a
single r-prefixed triple-quoted string at module top level.
"""

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Bridge Dashboard</title>
<script>try{var p=localStorage.getItem("ab-theme");
document.documentElement.setAttribute("data-theme",
  p==="light"||p==="dark"?p:(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"))}catch(e){}
/* Locale bootstrap: keep this resolver in sync with resolveSystemLocale() in
   the body script — it runs pre-render so <html lang> is right before first
   paint. The title map mirrors LOCALES[...]["app.title"]. For non-English
   locales the data-i18n fallback text is English, so [data-i18n] elements
   stay invisible until applyStatic() localizes them; the timeout failsafe
   reveals the fallback if the body script never runs. */
try{var _l=localStorage.getItem("ab-locale"),_lang="en";
if(_l==="en"||_l==="zh-CN"||_l==="zh-TW")_lang=_l;
else{var _ls=(navigator.languages&&navigator.languages.length?navigator.languages:[navigator.language])||[];
for(var _i=0;_i<_ls.length;_i++){var _t=String(_ls[_i]||"").toLowerCase();
if(/^zh/.test(_t)){_lang=/hant|tw|hk|mo/.test(_t)?"zh-TW":"zh-CN";break}
if(/^en/.test(_t)){_lang="en";break}}}
document.documentElement.lang=_lang;
document.title={en:"Agent Bridge Dashboard","zh-CN":"Agent Bridge 仪表板","zh-TW":"Agent Bridge 儀表板"}[_lang];
if(_lang!=="en"){document.documentElement.setAttribute("data-i18n-pending","");
setTimeout(function(){document.documentElement.removeAttribute("data-i18n-pending")},1500)}}catch(e){}</script>
<style>
:root{
  color-scheme:light;
  --font-sans:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC",
    "PingFang TC","Microsoft YaHei","Microsoft JhengHei","Noto Sans SC",
    "Noto Sans TC",sans-serif;
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
.week-total{display:flex;align-items:baseline;justify-content:space-between;
  gap:8px;padding:0 18px 10px;font-size:12px;color:var(--dim)}
.week-total .wt-val{font-variant-numeric:tabular-nums;font-weight:600;
  white-space:nowrap}
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
#langsel{flex:1;min-width:0;font:inherit;font-size:11px;color:var(--dim);
  background:var(--panel);border:1px solid var(--border);border-radius:7px;
  padding:3px 4px;cursor:pointer}
#langsel:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
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
.sdur,.hdur,.stok,.htok,.htps{color:var(--dimmer);font-variant-numeric:tabular-nums;
  white-space:nowrap}
/* ---------- pane header ---------- */
/* --railw is the ruler strip's width. --railin is the measured native
   scrollbar gutter (JS sets it): it insets the overlaid ruler from the
   pane's right edge and is reserved in the header/composer padding, so
   every inner column centers on the same axis as the conversation blocks —
   the center of the scrollbar-free viewport. */
#pane{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--panel);
  --railw:18px;--railin:0px}
#sesshead{display:flex;align-items:center;gap:14px;
  padding:16px calc(26px + var(--railin)) 14px 26px;
  border-bottom:1px solid var(--border);min-height:78px}
#menubtn{display:none;flex:none;width:34px;height:34px;align-items:center;
  justify-content:center;background:none;border:1px solid var(--border);
  border-radius:8px;color:var(--dim);cursor:pointer}
#menubtn:hover{background:var(--panel3)}
#hwrap{flex:1;display:flex;align-items:center;gap:14px;min-width:0;
  max-width:960px;margin:0 auto}
/* The header body is a four-row grid: the left column carries title,
   status metrics, repo · agent/model, and the raw id pair; the right
   column holds the 2x2 icon-only control cluster spanning the top two
   rows, then right-aligned turns and age cells matching rows three and
   four. */
.hgrid{flex:1;min-width:0;display:grid;column-gap:14px;row-gap:3px;
  align-items:center;grid-template-columns:minmax(0,1fr) auto;
  grid-template-areas:"title side" "status side" "info turns" "ids age"}
.htitle{grid-area:title;font-size:17px;font-weight:700;margin:0;line-height:1.3;
  display:flex;align-items:center;gap:8px;min-width:0}
.htext{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hstatus{grid-area:status;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-size:12px;font-weight:600;color:var(--text)}
.hinfo{grid-area:info;display:flex;align-items:baseline;gap:7px;min-width:0;
  margin-top:5px;font-size:12px;color:var(--dim)}
.hrepo{flex:0 1 auto;min-width:0;max-width:34ch;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.hagent{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.hsep{flex:none;color:var(--dimmer)}
/* The raw task/session id pair sits on its own dimmed mono line — it
   ellipsizes as a single row with the full pair kept on the tooltip. */
.hids{grid-area:ids;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-family:var(--font-mono);font-size:11px;
  font-weight:500;color:var(--dimmer)}
/* The four session controls form one 2x2 icon cluster inside the single
   role=group element: pause/resume on the title row, cancel/download on
   the status row. */
.hactions{grid-area:side;justify-self:end;display:grid;
  grid-template-columns:repeat(2,auto);gap:5px}
.hact{display:inline-flex;align-items:center;justify-content:center;
  width:28px;height:28px;padding:0;color:var(--dim);background:var(--panel);
  border:1px solid var(--border);border-radius:7px;cursor:pointer}
.hact:hover:not(:disabled){background:var(--panel3);color:var(--text)}
.hact:disabled{opacity:.45;cursor:default}
.hact:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.hact .ic{display:block}
.hturns{grid-area:turns;justify-self:end;margin-top:5px;font-size:11.5px;
  color:var(--dimmer);white-space:nowrap}
.hage{grid-area:age;justify-self:end;font-size:11.5px;color:var(--dimmer);
  white-space:nowrap}
.hplaceholder{color:var(--dimmer);font-size:14px}
/* ---------- conversation cards ---------- */
#conv{flex:1;min-height:0;position:relative;display:flex}
#content{flex:1;position:relative;overflow-y:auto;padding:20px 26px 24px;
  scrollbar-gutter:stable}
/* The ruler overlays the content viewport at its right edge, inset by the
   measured scrollbar gutter (--railin) so it sits immediately LEFT of the
   native scrollbar — Voyager ruler mode has no full-height spine, just the
   compact centered tick group. */
#rail{position:absolute;top:0;bottom:0;right:var(--railin);width:var(--railw);
  z-index:2}
/* Timeline ruler graduations: every tick is the same fixed 14px length,
   anchored at the rail's content-facing edge and centered on the compact
   group's center-relative offset via --my + translateY(-50%). Kind shows as
   thickness only — agent message 2px, dispatched message 3px, turn end 4px. */
.mark{position:absolute;top:0;left:0;width:14px;height:2px;padding:0;
  border:0;border-radius:2px;background:var(--dimmer);cursor:pointer;
  transform:translateY(var(--my,0px)) translateY(-50%);
  opacity:var(--mo,.5);
  transition:transform .11s cubic-bezier(.2,.8,.2,1),opacity .11s ease-out,
    background-color .15s ease}
.mark.k-disp{height:3px}
.mark.k-turn{height:4px}
/* Hit zone: extends toward the conversation only — never toward the
   scrollbar on the rail's right edge, so track clicks stay native. */
.mark::after{content:"";position:absolute;inset:-1px 0 -1px -4px}
.mark:hover{opacity:1}
.mark.cur{opacity:1;background:var(--accent)}
.mark:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.block{margin:0 auto 14px;max-width:960px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  padding:13px 16px}
.chead{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;
  color:var(--dim);margin-bottom:8px}
.chead .ctime{margin-left:auto;font-weight:400;color:var(--dimmer);font-size:11px}
.card.prompt .chead{color:var(--accent)}
.card.prompt.user{background:var(--accent-tint);border-left:3px solid var(--accent)}
.card pre.ptext{white-space:pre-wrap;word-break:break-word;margin:0;
  font:inherit;font-size:14px}
.card:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
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
#chatbar{border-top:1px solid var(--border);background:var(--panel);
  padding:14px calc(26px + var(--railin)) 14px 26px}
.chatinner{display:flex;gap:10px;align-items:flex-end;width:100%;
  max-width:960px;margin:0 auto}
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
/* Status line: its own row under the input on the same 960px axis, so it
   never skews the visible textarea/send group; collapses fully when empty
   while keeping the aria-live region mounted. */
#chatstatus{display:block;width:100%;max-width:960px;margin:8px auto 0;
  font-size:11.5px;color:var(--dim);text-align:right}
#chatstatus:empty{display:none}
/* Queued outbox records ride directly above the composer — one card per
   pending message, on the same 960px axis as the input row. */
#queue{display:flex;flex-direction:column;gap:6px;width:100%;
  max-width:960px;margin:0 auto 10px}
#queue[hidden]{display:none}
.qcard{display:flex;align-items:center;gap:8px;background:var(--panel2);
  border:1px solid var(--border);border-radius:10px;padding:5px 8px 5px 12px;
  font-size:12.5px;color:var(--text)}
.qicon{flex:none;color:var(--dimmer);display:inline-flex}
.qtext{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.qmeta{flex:none;font-size:11px;color:var(--dimmer);white-space:nowrap;
  font-variant-numeric:tabular-nums}
.qact{flex:none;height:26px;min-width:26px;display:inline-flex;align-items:center;
  justify-content:center;gap:5px;background:none;border:1px solid var(--border);
  border-radius:7px;color:var(--dim);cursor:pointer;font:inherit;font-size:11px;
  font-weight:600;padding:0}
.qact.qsteer{width:auto;padding:0 9px}
.qact:hover{background:var(--panel3);color:var(--text)}
.qact.qdel:hover{color:var(--red);border-color:var(--red)}
.qact:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.qact .ic{display:block}
.clamp{max-height:120px;overflow:hidden;position:relative}
.clamp::after{content:"";position:absolute;bottom:0;left:0;right:0;height:40px;
  background:linear-gradient(transparent,var(--panel))}
.expand{cursor:pointer;color:var(--accent);font:inherit;font-size:12px;margin-top:6px;
  display:inline-block;background:none;border:none;padding:0}
.expand:focus-visible{outline:2px solid var(--accent)}
#backdrop{display:none}
/* While a non-English locale is pending, hide the English fallback text in
   data-i18n elements (visibility only — no layout shift). */
html[data-i18n-pending] [data-i18n]{visibility:hidden}
@media (max-width:900px){
  #menubtn{display:flex}
  #sidebar{position:fixed;top:0;left:0;bottom:0;width:min(320px,85vw);z-index:40;
    transform:translateX(-105%);transition:transform .2s ease;
    box-shadow:4px 0 24px rgba(0,0,0,.15)}
  #sidebar.open{transform:none}
  #backdrop.open{display:block;position:fixed;inset:0;z-index:35;
    background:rgba(0,0,0,.35)}
  #pane{--railw:14px}
  #sesshead{padding:12px calc(16px + var(--railin)) 12px 16px;min-height:0}
  #content{padding:14px 16px}
  #chatbar{padding:10px calc(16px + var(--railin)) 10px 16px}
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
      <h2 data-i18n="nav.subagents">Sub Agents</h2>
      <span id="live" aria-live="polite"><span class="ldot"></span><span class="lt">connecting…</span></span>
    </div>
    <!-- Passive aggregate: visible semantic text + a shared title/aria-label
         sentence, deliberately no aria-live — it re-renders every poll. -->
    <div class="week-total" id="weektotal"><span data-i18n="tokens.week_label">This week</span><span class="wt-val"></span></div>
    <div id="sesslist" role="listbox" aria-label="Sessions"
      data-i18n-aria-label="a11y.sessions"></div>
    <div class="side-foot">
      <span id="themelbl" class="side-foot-label" data-i18n="theme.label">Theme</span>
      <div class="theme-options" role="radiogroup" aria-labelledby="themelbl">
        <button type="button" role="radio" data-theme-pref="system" aria-checked="true"
          tabindex="0" title="Follow system"
          data-i18n-title="theme.follow_system"><span data-i18n="theme.system">System</span></button>
        <button type="button" role="radio" data-theme-pref="light" aria-checked="false"
          tabindex="-1"><span data-i18n="theme.light">Light</span></button>
        <button type="button" role="radio" data-theme-pref="dark" aria-checked="false"
          tabindex="-1"><span data-i18n="theme.dark">Dark</span></button>
      </div>
    </div>
    <div class="side-foot">
      <label id="langlbl" class="side-foot-label" for="langsel"
        data-i18n="language.label">Language</label>
      <select id="langsel">
        <option value="system" data-i18n="language.system">Follow system</option>
        <option value="en">English</option>
        <option value="zh-CN">简体中文</option>
        <option value="zh-TW">繁體中文</option>
      </select>
    </div>
  </aside>
  <div id="backdrop"></div>
  <div id="pane">
    <header id="sesshead">
      <button id="menubtn" type="button" aria-label="Show session list"
        data-i18n-aria-label="a11y.show_session_list"
        aria-controls="sidebar" aria-expanded="false"><svg class="ic" width="16" height="16"
        viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M3 6h18"/><path d="M3 12h18"/><path d="M3 18h18"/></svg></button>
      <div id="hwrap"><div class="hplaceholder"
        data-i18n="session.select">Select a session</div></div>
    </header>
    <div id="conv">
      <div id="content"><div class="empty"
        data-i18n="session.select">Select a session</div></div>
      <nav id="rail" aria-label="Message positions"
        data-i18n-aria-label="rail.label" hidden></nav>
    </div>
    <div id="chatbar">
      <div id="queue" role="list" hidden
        aria-label="Queued messages" data-i18n-aria-label="queue.label"></div>
      <div class="chatinner">
        <textarea id="chatinput" rows="1" disabled
          placeholder="Send an instruction… (Enter to send, Shift+Enter for newline)"
          data-i18n-placeholder="chat.placeholder.default"
          aria-label="Message the selected session"
          data-i18n-aria-label="a11y.message_selected_session"></textarea>
        <button id="chatsend" type="button" disabled aria-label="Send message"
          data-i18n-aria-label="a11y.send_message" title="Send"
          data-i18n-title="chat.send"><svg class="ic"
          width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
          focusable="false"><path d="M3 12h18m-9-9l9 9-9 9"/></svg></button>
      </div>
      <span id="chatstatus" aria-live="polite"></span>
    </div>
  </div>
</div>
<script>
"use strict";
const $=s=>document.querySelector(s);
let sessions=[], tasks=[], selected=null;
let offsets={};            // per-session byte offset for incremental reads
let eventsCache={};        // per-session array of normalized events (for fast re-render)
let liveUsage={};          // session_id -> latest consumed snapshot from "usage" events
let liveAll={};            // session_id -> {task_id, consumed} batched in /api/overview
let pollTimer=null, ovTimer=null;

/* ---------- presence heartbeat (bridge pops us up only when no tab is open) ---------- */
// Fresh id per page load: a duplicated tab must not share the lease or let
// its pagehide beacon remove a sibling's presence.
const clientId=crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random();
function ping(bye){
  if(bye){try{navigator.sendBeacon(`/api/presence?id=${clientId}&bye=1`)}catch(e){}}
  else fetch(`/api/presence?id=${clientId}`).catch(()=>{});
}
ping(0);setInterval(()=>ping(0),4000);
addEventListener("pagehide",()=>ping(1));
// A throttled/frozen interval can lag ~60s; re-register as soon as the tab
// runs again (bfcache restore or reload, resurfacing, network recovery).
addEventListener("pageshow",()=>ping(0));
addEventListener("focus",()=>ping(0));
addEventListener("online",()=>ping(0));
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")ping(0)});

const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* ---------- i18n: bundled dictionaries (en / zh-CN / zh-TW) ----------
   All dashboard chrome comes from these checked-in dictionaries — never from
   runtime machine translation. User content (prompts, agent/thinking text,
   paths, titles, ids, provider errors) is data and is never localized.
   `ab-locale` stores the preference: "system" (default) resolves
   navigator.languages; explicit locales override it. Missing keys fall back
   to English. Values are plain text templates with {named} placeholders —
   the JSON-compatible literal is parsed by tests for completeness. */
const LOCALES=Object.freeze({
"en":{
"app.title":"Agent Bridge Dashboard",
"nav.subagents":"Sub Agents",
"rail.label":"Message positions",
"rail.agent_message":"Agent message",
"rail.dispatched_message":"Dispatched message",
"rail.turn_end":"Turn end",
"a11y.sessions":"Sessions",
"a11y.show_session_list":"Show session list",
"a11y.message_selected_session":"Message the selected session",
"a11y.send_message":"Send message",
"theme.label":"Theme",
"theme.follow_system":"Follow system",
"theme.system":"System",
"theme.light":"Light",
"theme.dark":"Dark",
"language.label":"Language",
"language.system":"Follow system",
"session.select":"Select a session",
"session.working_repo":"Working repo · {repo}",
"session.task_id":"task {id}",
"session.session_id":"session {id}",
"session.turns":{"one":"{n} turn","other":"{n} turns"},
"empty.no_sessions":"No sessions",
"empty.no_events":"No events",
"empty.loading":"Loading…",
"status.connecting":"connecting…",
"status.live":"live",
"status.disconnected":"disconnected",
"status.proc.running":"Running",
"status.proc.starting":"Starting",
"status.proc.ready":"Ready",
"status.proc.idle":"Idle",
"status.proc.done":"Done",
"status.proc.failed":"Failed",
"status.proc.unknown":"Unknown",
"chat.placeholder.default":"Send an instruction… (Enter to send, Shift+Enter for newline)",
"chat.placeholder.session":"Send an instruction to {session}…",
"chat.placeholder.short":"Send an instruction…",
"chat.send":"Send",
"send.sending":"sending…",
"send.queued":"queued…",
"send.waiting_busy":"waiting for agent — session busy…",
"send.waiting_owner":"waiting for agent's bridge…",
"send.delivering":"delivering…",
"send.dispatched":"dispatched",
"send.dispatched_task":"dispatched · {task}",
"send.failed":"send failed",
"send.err_expired":"message expired in queue",
"send.err_unknown_session":"unknown session",
"send.err_missing":"message is no longer queued",
"send.err_empty_or_too_long":"message empty or too long",
"send.err_bad_session":"bad session",
"send.err_bad_request":"bad request",
"send.err_invalid_record":"invalid queued message",
"send.err_dispatch_failed":"dispatch failed",
"send.err_dispatch_error":"dispatch error",
"a11y.task_actions":"Task actions",
"queue.label":"Queued messages",
"queue.steer":"Steer",
"queue.steer_hint":"Return to composer for editing",
"queue.delete":"Remove from queue",
"queue.recalled":"moved back to composer",
"queue.deleted":"removed from queue",
"queue.err_delivering":"already being delivered",
"queue.err_dispatched":"already dispatched",
"queue.err_failed":"queue operation failed",
"act.pause":"Pause",
"act.cancel":"Cancel",
"act.resume":"Resume",
"act.requesting":"requesting…",
"act.pending":"request in flight…",
"act.paused":"task paused",
"act.cancelled":"task cancelled",
"act.download":"Download",
"act.download_hint":"Download transcript as Markdown",
"act.resumed":"resumed · {task}",
"act.failed":"action failed",
"act.err_expired":"request expired in queue",
"act.err_unknown_task":"unknown task",
"act.err_wrong_session":"task belongs to another session",
"act.no_active":"no active task",
"act.no_resumable":"nothing to resume",
"act.no_transcript":"no transcript yet",
"dl.default_title":"Transcription",
"transcript.user_message":"User Message",
"transcript.dispatched_message":"Dispatched Message",
"transcript.agent":"Agent",
"transcript.thinking":"Thinking",
"transcript.thinking_words":"Thinking · {n} words",
"transcript.thinking_chars":"Thinking · {n} chars",
"transcript.show_more":"show more",
"transcript.turn_ended":"turn ended",
"transcript.error":"error",
"tool.kind.tool":"Tool",
"tool.kind.execute":"Execute",
"tool.kind.read":"Read",
"tool.kind.search":"Search",
"tool.kind.edit":"Edit",
"tool.kind.fetch":"Fetch",
"tool.input":"input",
"tool.status.in_progress":"in progress",
"tool.status.completed":"completed",
"tool.status.failed":"failed",
"tool.status.error":"error",
"stop.stalled":"stalled",
"stop.cancelled":"cancelled",
"stop.paused":"paused",
"stop.error":"error",
"time.dur_sec":"{n}s",
"time.dur_min_sec":"{m}m {s}s",
"time.dur_min":"{m}m",
"time.dur_hour_min":"{h}h {m}m",
"time.dur_hour":"{h}h",
"time.dur_ms":"{n}ms",
"time.ago_sec":"{n}s ago",
"time.ago_min":"{n}m ago",
"time.ago_hour":"{n}h ago",
"time.ago_day":"{n}d ago",
"tokens.run":"Run tokens {n}",
"tokens.last_snapshot":"Last snapshot estimate ~{n} — not a whole-run total",
"tokens.ctx_only":"Context in use ~{n} — last snapshot only, not a run total",
"tokens.in":"in {n}",
"tokens.cached":"cached {n}",
"tokens.out":"out {n}",
"tokens.context":"context {n}",
"tokens.conversation":"conversation {n}",
"tokens.live":"live",
"tokens.estimate":"estimate",
"tokens.unit":"tok",
"tokens.per_sec":"{n} tok/s",
"tokens.out_speed":"Output speed: {rate} ({out} output tokens in {dur})",
"tokens.week_label":"This week",
"tokens.week_title":{"one":"Tokens since {date} · {n} task","other":"Tokens since {date} · {n} tasks"},
"tokens.week_scope":"based on retained Agent Bridge task history",
"tokens.week_approx":"includes estimated or incomplete history",
"tokens.week_excluded":{"one":"{n} task excluded — no usable timestamp","other":"{n} tasks excluded — no usable timestamp"},
"tokens.week_live":"includes live in-progress usage"
},
"zh-CN":{
"app.title":"Agent Bridge 仪表板",
"nav.subagents":"子 Agent",
"rail.label":"消息位置",
"rail.agent_message":"Agent 消息",
"rail.dispatched_message":"已派发消息",
"rail.turn_end":"回合结束",
"a11y.sessions":"会话",
"a11y.show_session_list":"显示会话列表",
"a11y.message_selected_session":"向所选会话发送消息",
"a11y.send_message":"发送消息",
"theme.label":"主题",
"theme.follow_system":"跟随系统",
"theme.system":"系统",
"theme.light":"浅色",
"theme.dark":"深色",
"language.label":"语言",
"language.system":"跟随系统",
"session.select":"选择一个会话",
"session.working_repo":"工作仓库 · {repo}",
"session.task_id":"任务 {id}",
"session.session_id":"会话 {id}",
"session.turns":{"other":"{n} 回合"},
"empty.no_sessions":"暂无会话",
"empty.no_events":"暂无事件",
"empty.loading":"加载中…",
"status.connecting":"连接中…",
"status.live":"实时",
"status.disconnected":"已断开",
"status.proc.running":"运行中",
"status.proc.starting":"启动中",
"status.proc.ready":"就绪",
"status.proc.idle":"空闲",
"status.proc.done":"已完成",
"status.proc.failed":"失败",
"status.proc.unknown":"未知",
"chat.placeholder.default":"发送指令…（Enter 发送，Shift+Enter 换行）",
"chat.placeholder.session":"向 {session} 发送指令…",
"chat.placeholder.short":"发送指令…",
"chat.send":"发送",
"send.sending":"发送中…",
"send.queued":"已排队…",
"send.waiting_busy":"等待 Agent——会话正忙…",
"send.waiting_owner":"等待 Agent 的 Bridge…",
"send.delivering":"投递中…",
"send.dispatched":"已派发",
"send.dispatched_task":"已派发 · {task}",
"send.failed":"发送失败",
"send.err_expired":"消息已在队列中过期",
"send.err_unknown_session":"未知会话",
"send.err_missing":"消息已不在队列中",
"send.err_empty_or_too_long":"消息为空或过长",
"send.err_bad_session":"无效会话",
"send.err_bad_request":"无效请求",
"send.err_invalid_record":"无效的队列消息",
"send.err_dispatch_failed":"派发失败",
"send.err_dispatch_error":"派发错误",
"a11y.task_actions":"任务操作",
"queue.label":"排队消息",
"queue.steer":"移回",
"queue.steer_hint":"移回输入框编辑",
"queue.delete":"从队列移除",
"queue.recalled":"已放回输入框",
"queue.deleted":"已从队列移除",
"queue.err_delivering":"正在投递中",
"queue.err_dispatched":"已派发",
"queue.err_failed":"队列操作失败",
"act.pause":"暂停",
"act.cancel":"取消",
"act.resume":"恢复",
"act.requesting":"请求中…",
"act.pending":"请求进行中…",
"act.paused":"任务已暂停",
"act.cancelled":"任务已取消",
"act.download":"下载",
"act.download_hint":"下载转录为 Markdown",
"act.resumed":"已恢复 · {task}",
"act.failed":"操作失败",
"act.err_expired":"请求已在队列中过期",
"act.err_unknown_task":"未知任务",
"act.err_wrong_session":"任务属于其他会话",
"act.no_active":"没有运行中的任务",
"act.no_resumable":"没有可恢复的任务",
"act.no_transcript":"暂无转录内容",
"dl.default_title":"转录",
"transcript.user_message":"用户消息",
"transcript.dispatched_message":"已派发消息",
"transcript.agent":"Agent 消息",
"transcript.thinking":"思考中",
"transcript.thinking_words":"思考中 · {n} 词",
"transcript.thinking_chars":"思考中 · {n} 字",
"transcript.show_more":"显示更多",
"transcript.turn_ended":"回合已结束",
"transcript.error":"错误",
"tool.kind.tool":"工具",
"tool.kind.execute":"执行",
"tool.kind.read":"读取",
"tool.kind.search":"搜索",
"tool.kind.edit":"编辑",
"tool.kind.fetch":"获取",
"tool.input":"输入",
"tool.status.in_progress":"进行中",
"tool.status.completed":"已完成",
"tool.status.failed":"失败",
"tool.status.error":"错误",
"stop.stalled":"已停滞",
"stop.cancelled":"已取消",
"stop.paused":"已暂停",
"stop.error":"错误",
"time.dur_sec":"{n} 秒",
"time.dur_min_sec":"{m} 分 {s} 秒",
"time.dur_min":"{m} 分",
"time.dur_hour_min":"{h} 小时 {m} 分",
"time.dur_hour":"{h} 小时",
"time.dur_ms":"{n} 毫秒",
"time.ago_sec":"{n} 秒前",
"time.ago_min":"{n} 分钟前",
"time.ago_hour":"{n} 小时前",
"time.ago_day":"{n} 天前",
"tokens.run":"运行 token {n}",
"tokens.last_snapshot":"最近快照估算 ~{n}——不是单次运行总量",
"tokens.ctx_only":"上下文占用 ~{n}——仅为最近快照，不是单次运行总量",
"tokens.in":"输入 {n}",
"tokens.cached":"已缓存 {n}",
"tokens.out":"输出 {n}",
"tokens.context":"上下文 {n}",
"tokens.conversation":"会话累计 {n}",
"tokens.live":"实时",
"tokens.estimate":"估算",
"tokens.unit":"token",
"tokens.per_sec":"{n} token/秒",
"tokens.out_speed":"输出速率: {rate}（{dur} 内输出 {out} token）",
"tokens.week_label":"本周",
"tokens.week_title":{"other":"自 {date} 以来的 token · {n} 个任务"},
"tokens.week_scope":"基于已保留的 Agent Bridge 任务历史",
"tokens.week_approx":"包含估算或不完整历史",
"tokens.week_excluded":{"other":"{n} 个任务已排除——无可用时间戳"},
"tokens.week_live":"包含进行中的实时用量"
},
"zh-TW":{
"app.title":"Agent Bridge 儀表板",
"nav.subagents":"子 Agent",
"rail.label":"訊息位置",
"rail.agent_message":"Agent 訊息",
"rail.dispatched_message":"已派發訊息",
"rail.turn_end":"回合結束",
"a11y.sessions":"工作階段",
"a11y.show_session_list":"顯示工作階段清單",
"a11y.message_selected_session":"傳送訊息給所選工作階段",
"a11y.send_message":"傳送訊息",
"theme.label":"主題",
"theme.follow_system":"跟隨系統",
"theme.system":"系統",
"theme.light":"淺色",
"theme.dark":"深色",
"language.label":"語言",
"language.system":"跟隨系統",
"session.select":"選擇一個工作階段",
"session.working_repo":"工作儲存庫 · {repo}",
"session.task_id":"任務 {id}",
"session.session_id":"工作階段 {id}",
"session.turns":{"other":"{n} 回合"},
"empty.no_sessions":"暫無工作階段",
"empty.no_events":"暫無事件",
"empty.loading":"載入中…",
"status.connecting":"連線中…",
"status.live":"即時",
"status.disconnected":"已中斷",
"status.proc.running":"執行中",
"status.proc.starting":"啟動中",
"status.proc.ready":"就緒",
"status.proc.idle":"閒置",
"status.proc.done":"已完成",
"status.proc.failed":"失敗",
"status.proc.unknown":"未知",
"chat.placeholder.default":"傳送指令…（Enter 傳送，Shift+Enter 換行）",
"chat.placeholder.session":"向 {session} 傳送指令…",
"chat.placeholder.short":"傳送指令…",
"chat.send":"傳送",
"send.sending":"傳送中…",
"send.queued":"已排隊…",
"send.waiting_busy":"等待 Agent——工作階段忙碌中…",
"send.waiting_owner":"等待 Agent 的 Bridge…",
"send.delivering":"傳遞中…",
"send.dispatched":"已派發",
"send.dispatched_task":"已派發 · {task}",
"send.failed":"傳送失敗",
"send.err_expired":"訊息已在佇列中過期",
"send.err_unknown_session":"未知工作階段",
"send.err_missing":"訊息已不在佇列中",
"send.err_empty_or_too_long":"訊息為空或過長",
"send.err_bad_session":"無效工作階段",
"send.err_bad_request":"無效請求",
"send.err_invalid_record":"無效的佇列訊息",
"send.err_dispatch_failed":"派發失敗",
"send.err_dispatch_error":"派發錯誤",
"a11y.task_actions":"任務操作",
"queue.label":"佇列訊息",
"queue.steer":"移回",
"queue.steer_hint":"移回輸入框編輯",
"queue.delete":"從佇列移除",
"queue.recalled":"已放回輸入框",
"queue.deleted":"已從佇列移除",
"queue.err_delivering":"正在傳遞中",
"queue.err_dispatched":"已派發",
"queue.err_failed":"佇列操作失敗",
"act.pause":"暫停",
"act.cancel":"取消",
"act.resume":"恢復",
"act.requesting":"請求中…",
"act.pending":"請求進行中…",
"act.paused":"任務已暫停",
"act.cancelled":"任務已取消",
"act.download":"下載",
"act.download_hint":"下載轉錄為 Markdown",
"act.resumed":"已恢復 · {task}",
"act.failed":"操作失敗",
"act.err_expired":"請求已在佇列中過期",
"act.err_unknown_task":"未知任務",
"act.err_wrong_session":"任務屬於其他工作階段",
"act.no_active":"沒有執行中的任務",
"act.no_resumable":"沒有可恢復的任務",
"act.no_transcript":"暫無轉錄內容",
"dl.default_title":"轉錄",
"transcript.user_message":"使用者訊息",
"transcript.dispatched_message":"已派發訊息",
"transcript.agent":"Agent 訊息",
"transcript.thinking":"思考中",
"transcript.thinking_words":"思考中 · {n} 詞",
"transcript.thinking_chars":"思考中 · {n} 字",
"transcript.show_more":"顯示更多",
"transcript.turn_ended":"回合已結束",
"transcript.error":"錯誤",
"tool.kind.tool":"工具",
"tool.kind.execute":"執行",
"tool.kind.read":"讀取",
"tool.kind.search":"搜尋",
"tool.kind.edit":"編輯",
"tool.kind.fetch":"擷取",
"tool.input":"輸入",
"tool.status.in_progress":"進行中",
"tool.status.completed":"已完成",
"tool.status.failed":"失敗",
"tool.status.error":"錯誤",
"stop.stalled":"已停滯",
"stop.cancelled":"已取消",
"stop.paused":"已暫停",
"stop.error":"錯誤",
"time.dur_sec":"{n} 秒",
"time.dur_min_sec":"{m} 分 {s} 秒",
"time.dur_min":"{m} 分",
"time.dur_hour_min":"{h} 小時 {m} 分",
"time.dur_hour":"{h} 小時",
"time.dur_ms":"{n} 毫秒",
"time.ago_sec":"{n} 秒前",
"time.ago_min":"{n} 分鐘前",
"time.ago_hour":"{n} 小時前",
"time.ago_day":"{n} 天前",
"tokens.run":"執行 token {n}",
"tokens.last_snapshot":"最近快照估算 ~{n}——不是單次執行總量",
"tokens.ctx_only":"上下文佔用 ~{n}——僅為最近快照，不是單次執行總量",
"tokens.in":"輸入 {n}",
"tokens.cached":"已快取 {n}",
"tokens.out":"輸出 {n}",
"tokens.context":"上下文 {n}",
"tokens.conversation":"工作階段累計 {n}",
"tokens.live":"即時",
"tokens.estimate":"估算",
"tokens.unit":"token",
"tokens.per_sec":"{n} token/秒",
"tokens.out_speed":"輸出速率: {rate}（{dur} 內輸出 {out} token）",
"tokens.week_label":"本週",
"tokens.week_title":{"other":"自 {date} 以來的 token · {n} 個任務"},
"tokens.week_scope":"基於已保留的 Agent Bridge 任務歷史",
"tokens.week_approx":"包含估算或不完整歷史",
"tokens.week_excluded":{"other":"{n} 個任務已排除——無可用時間戳"},
"tokens.week_live":"包含進行中的即時用量"
}
});
const LOCALE_KEY="ab-locale";
const LOCALE_PREFS=["system","en","zh-CN","zh-TW"];
function localePref(){
  try{const p=localStorage.getItem(LOCALE_KEY);
    return LOCALE_PREFS.includes(p)?p:"system"}catch(e){return"system"}
}
/* zh-Hant/TW/HK/MO -> zh-TW; zh/Hans/CN/SG/MY -> zh-CN; en-* -> en;
   anything else -> en. First recognized tag in preference order wins.
   The head bootstrap mirrors this so <html lang> is right pre-render —
   keep both copies in sync. */
function resolveSystemLocale(){
  let ls=[];
  try{ls=navigator.languages&&navigator.languages.length?[...navigator.languages]
    :[navigator.language]}catch(e){}
  for(const raw of ls){
    const tag=String(raw||"").toLowerCase().replace(/_/g,"-");
    if(/^zh/.test(tag))return/hant|tw|hk|mo/.test(tag)?"zh-TW":"zh-CN";
    if(/^en/.test(tag))return"en";
  }
  return"en";
}
let langPref=localePref();
let locale=langPref==="system"?resolveSystemLocale():langPref;
function pluralForm(n){
  try{return new Intl.PluralRules(locale).select(n)}catch(e){}
  return n===1?"one":"other";
}
function t(key,params){
  let m=(LOCALES[locale]||LOCALES.en)[key];
  if(m===undefined)m=LOCALES.en[key];
  if(m===undefined)return key;
  if(m&&typeof m==="object"){
    const n=params&&Number.isFinite(params.n)?params.n:0;
    const f=n===1&&m.one!==undefined?"one":pluralForm(n);
    m=m[f]!==undefined?m[f]:(m.other!==undefined?m.other:
      m.one!==undefined?m.one:"");
  }
  return String(m).replace(/\{(\w+)\}/g,(s,k)=>
    params&&params[k]!=null?String(params[k]):s);
}
const fmtNum=v=>{try{return Number(v).toLocaleString(locale)}catch(e){return String(v)}};
/* Missing/unparseable timestamps render nothing — never an English
   "Invalid Date" or a NaN count like "NaN 天前". */
const fmtTs=v=>{if(v==null)return"";
  const d=new Date(v);if(!Number.isFinite(+d))return"";
  try{return d.toLocaleTimeString(locale)}catch(e){
  try{return d.toLocaleTimeString()}catch(e2){return""}}};
const ago=v=>{if(v==null)return"";
  const s=(Date.now()-new Date(v))/1e3;if(!Number.isFinite(s))return"";
  const c=Math.max(0,s);                  // future ts (clock skew) -> "0s ago"
  if(c<60)return t("time.ago_sec",{n:Math.floor(c)});
  if(c<3600)return t("time.ago_min",{n:Math.floor(c/60)});
  if(c<86400)return t("time.ago_hour",{n:Math.floor(c/3600)});
  return t("time.ago_day",{n:Math.floor(c/86400)})};
function applyStatic(){
  document.querySelectorAll("[data-i18n]").forEach(el=>{
    el.textContent=t(el.dataset.i18n)});
  document.querySelectorAll("[data-i18n-title]").forEach(el=>{
    el.title=t(el.dataset.i18nTitle)});
  document.querySelectorAll("[data-i18n-aria-label]").forEach(el=>{
    el.setAttribute("aria-label",t(el.dataset.i18nAriaLabel))});
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el=>{
    el.setAttribute("placeholder",t(el.dataset.i18nPlaceholder))});
  document.documentElement.removeAttribute("data-i18n-pending");
}

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
  pause:['<rect x="6" y="4" width="4" height="16" rx="1"/>','<rect x="14" y="4" width="4" height="16" rx="1"/>'],
  play:['<polygon points="6 3 20 12 6 21 6 3"/>'],
  trash:['<path d="M3 6h18"/>','<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>','<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>','<line x1="10" y1="11" x2="10" y2="17"/>','<line x1="14" y1="11" x2="14" y2="17"/>'],
  steer:['<polyline points="9 10 4 15 9 20"/>','<path d="M20 4v7a4 4 0 0 1-4 4H4"/>'],
  queueMsg:['<polyline points="15 10 20 15 15 20"/>','<path d="M4 4v7a4 4 0 0 0 4 4h12"/>'],
  download:['<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>','<polyline points="7 10 12 15 17 10"/>','<line x1="12" y1="15" x2="12" y2="3"/>'],
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
  tasks.forEach((tk,i)=>{
    if(tk.session_id!==sid)return;
    const ts=Date.parse(tk.created_at);
    const v=Number.isFinite(ts)?ts:-Infinity;
    if(v>bestTs||(v===bestTs&&i>bestIdx)){best=tk;bestTs=v;bestIdx=i}
  });
  return best;
}
const PROC_STATUS=Object.freeze({
  busy:{key:"status.proc.running",tone:"running"},
  spawning:{key:"status.proc.starting",tone:"running"},
  ready:{key:"status.proc.ready",tone:"neutral"},
  idle_unloaded:{key:"status.proc.idle",tone:"neutral"},
});
function statusOf(s){
  const st=s.proc_state;
  if(st==="dead"){
    const tk=latestTask(s.session_id);
    if(tk&&tk.status==="failed")return{key:"status.proc.failed",tone:"error"};
    return{key:"status.proc.done",tone:"success"};
  }
  const m=PROC_STATUS[st];
  // Unrecognized proc_state: localized "Unknown", raw code kept as a detail.
  return m?{key:m.key,tone:m.tone}:{key:"status.proc.unknown",tone:"neutral",raw:st||""};
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
  if(s<60)return t("time.dur_sec",{n:s});
  const m=Math.floor(s/60),rs=s%60;
  if(m<60)return rs?t("time.dur_min_sec",{m,s:rs}):t("time.dur_min",{m});
  const h=Math.floor(m/60),rm=m%60;
  return rm?t("time.dur_hour_min",{h,m:rm}):t("time.dur_hour",{h})};
function taskDur(tk){
  if(!tk)return null;
  const start=Date.parse(tk.started_at);
  if(!Number.isFinite(start))return null;
  if(tk.status==="queued"||tk.status==="running")return{start,end:null};
  const end=Date.parse(tk.finished_at);
  if(!Number.isFinite(end))return null;
  return{start,end};
}
function durText(d){
  const s=fmtDur((d.end===null?Date.now():d.end)-d.start);
  return s?" · "+s:"";
}
function durSpan(tk,cls){
  const d=taskDur(tk);if(!d)return"";
  return `<span class="${cls}" data-tid="${esc(tk.task_id)}">${durText(d)}</span>`;
}

/* ---------- task token usage (latest task only) ----------
   `run_usage` is the accumulated token consumption of one run — one task /
   one worker turn, root agent plus subagent streams. Headline = input +
   output: conventional accounting where cached reads are part of input and
   reasoning output is already inside output. `used`/`size` are context-
   window occupancy snapshots — metadata only, never the consumed total.
   `quality:"estimate"` marks degraded derivations (resumed conversation
   counters without a baseline). Legacy `usage` rows are last-snapshot data:
   usable only labeled as a last-snapshot estimate, and a `used`-only
   snapshot is context occupancy, never a run total. */
function usageNums(u){
  if(!u||typeof u!=="object")u={};
  const m=typeof u._meta==="object"&&u._meta?u._meta:{};
  const num=v=>Number.isFinite(v)&&v>=0?v:null;
  const pick=(...keys)=>{for(const k of keys)for(const src of[u,m]){
    const v=num(src[k]);if(v!==null)return v}return null};
  return{
    input:pick("input","input_tokens","inputTokens","cognition.ai/inputTokens"),
    cached:pick("cached_read","cached_input_tokens","cachedInputTokens","cachedReadTokens","cognition.ai/cachedReadTokens"),
    cachedWrite:pick("cached_write","cached_write_tokens","cachedWriteTokens","cognition.ai/cachedWriteTokens"),
    output:pick("output","output_tokens","outputTokens","cognition.ai/outputTokens"),
    total:pick("total","total_tokens","totalTokens","used","cognition.ai/totalTokens"),
    used:pick("used"),
    size:pick("size"),
  };
}
function tokCount(u){
  const n=usageNums(u);
  if(n.input===null&&n.output===null)return n.total;
  return Math.max(n.input-(n.cached||0),0)+n.output;
}
/* Canonical run_usage -> display row. Headline is input + output (or the
   persisted total); `used` never feeds it. */
function runTok(u){
  if(!u||typeof u!=="object")return null;
  const n=usageNums(u);
  const total=(n.input!==null||n.output!==null)?(n.input||0)+(n.output||0)
    :(Number.isFinite(u.total)?u.total:null);
  if(total===null)return null;
  const conv=u.conversation_total;
  return{total,input:n.input,cached:n.cached,output:n.output,used:n.used,size:n.size,
    quality:u.quality==="estimate"?"estimate":"exact",
    conv:conv&&Number.isFinite(conv.total)?conv.total:null};
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
/* Single place composing the token tooltip/aria text. Compact counts stay
   locale-stable ("1m100k"); labels come from the dictionary and numbers use
   locale-aware grouping. */
function tokTitle(o){
  if(o.ctxOnly)
    return t("tokens.ctx_only",{n:fmtNum(o.total)});
  const seg=[];
  if(o.input!=null)seg.push(t("tokens.in",{n:fmtNum(o.input)}));
  if(o.cached)seg.push(t("tokens.cached",{n:fmtNum(o.cached)}));
  if(o.output!=null)seg.push(t("tokens.out",{n:fmtNum(o.output)}));
  // A legacy raw-usage fallback is a last-snapshot estimate, never a run
  // total — the lead says so instead of "Run tokens", and already carries
  // the estimate mark.
  const parts=[(o.legacy?t("tokens.last_snapshot",{n:fmtNum(o.total)})
    :t("tokens.run",{n:fmtNum(o.total)}))+
    (seg.length?" ("+seg.join(", ")+")":"")];
  if(o.used!=null)parts.push(t("tokens.context",
    {n:fmtNum(o.used)+(o.size?"/"+fmtNum(o.size):"")}));
  if(o.conv!=null)parts.push(t("tokens.conversation",{n:fmtNum(o.conv)}));
  if(o.live)parts.push(t("tokens.live"));
  if(o.estimate&&!o.legacy)parts.push(t("tokens.estimate"));
  return parts.join(" · ");
}
/* Token span for the latest/current task. Priority while running: live
   `usage` event snapshot, then the persisted run_usage. A present run_usage
   is authoritative — when it has no computable run total (estimate without
   counters) nothing renders rather than showing the conversation total.
   Old state falls back to raw `usage`, labeled a last-snapshot estimate
   rather than a run total. */
function tokSpan(tk,cls,live){
  let info=null,isLive=false,ctxOnly=false;
  if(tk&&(tk.status==="running"||tk.status==="queued")&&live){
    info=runTok(live);isLive=!!info;
  }
  const hasRun=tk&&tk.run_usage&&Object.keys(tk.run_usage).length>0;
  if(!info&&hasRun)info=runTok(tk.run_usage);
  if(!info&&!hasRun&&tk&&tk.usage&&typeof tk.usage==="object"){
    const m=tokCount(tk.usage);
    if(m!==null){
      const n=usageNums(tk.usage);
      ctxOnly=n.input===null&&n.output===null&&tk.usage.total===undefined&&
        tk.usage.total_tokens===undefined&&tk.usage.totalTokens===undefined&&
        !(tk.usage._meta&&tk.usage._meta["cognition.ai/totalTokens"]!==undefined);
      info={total:m,input:n.input,cached:n.cached,output:n.output,
        used:n.used,size:n.size,quality:"estimate",legacy:true};
    }
  }
  if(!info||!Number.isFinite(info.total))return"";
  const s=fmtTok(info.total);if(!s)return"";
  const est=info.quality==="estimate";
  const title=tokTitle({...info,live:isLive,estimate:est,ctxOnly});
  return `<span class="${cls}" title="${esc(title)}" aria-label="${esc(title)}"> · ${est?"~":""}${s} ${t("tokens.unit")}</span>`;
}
/* Output tokens per second for the latest task — output tokens only (never
   input/prefill) over elapsed task time. While queued/running the live
   consumed snapshot leads, then the persisted run_usage; terminal tasks
   read run_usage with the same legacy raw-usage fallback tokSpan uses. The
   interval is taskDur's started_at->finished_at clock (Date.now() while
   active), so terminal rates freeze permanently. Missing/nonpositive
   output, unparseable timestamps, inverted intervals and sub-second runs
   render nothing rather than a spike. */
function calcTps(tk,live){
  if(!tk)return null;
  let out=null;
  if((tk.status==="running"||tk.status==="queued")&&live){
    const info=runTok(live);
    if(info)out=info.output;
  }
  const hasRun=tk.run_usage&&typeof tk.run_usage==="object"&&
    Object.keys(tk.run_usage).length>0;
  if(out===null&&hasRun){
    const info=runTok(tk.run_usage);
    if(info)out=info.output;
  }
  if(out===null&&!hasRun&&tk.usage&&typeof tk.usage==="object")
    out=usageNums(tk.usage).output;
  if(!Number.isFinite(out)||out<=0)return null;
  const d=taskDur(tk);
  if(!d)return null;
  const sec=((d.end===null?Date.now():d.end)-d.start)/1e3;
  if(sec<1)return null;
  const rate=out/sec;
  if(!Number.isFinite(rate)||rate<=0)return null;
  return{rate,output:out,sec};
}
function tpsSpan(tk,cls,live){
  const res=calcTps(tk,live);
  if(!res)return"";
  const r=res.rate>=10?Math.round(res.rate):res.rate.toFixed(1);
  const text=t("tokens.per_sec",{n:r});
  const title=t("tokens.out_speed",{rate:text,out:fmtNum(res.output),
    dur:fmtDur(res.sec*1e3)});
  return `<span class="${cls}" title="${esc(title)}" aria-label="${esc(title)}"> · ${esc(text)}</span>`;
}
function tickDurations(){
  document.querySelectorAll("[data-tid]").forEach(el=>{
    const d=taskDur(tasks.find(x=>x.task_id===el.dataset.tid));
    el.textContent=d?durText(d):"";
  });
}
const baseName=p=>(p||"").split(/[\\/]/).filter(Boolean).pop()||"";

/* ---------- weekly token total + batched live partials ----------
   The browser-local calendar week runs Monday 00:00 to now; each unique
   task attributes wholly to the week it started (started_at, falling back
   to created_at). Local-civil Date construction keeps the boundary DST-
   safe. Per-task precedence: a non-empty run_usage is authoritative (the
   live partial is never added on top of it), then the pinned live partial
   for a still-running task, then the legacy last-snapshot estimate.
   Quality is independent of source: v===1+exact+non-partial is the only
   unqualified count, for final rows AND live snapshots alike — an
   unversioned/partial/estimate live partial from an older bridge still
   counts and still flags "~"; a live contribution always adds the
   in-progress note too. */
function weekStartMs(now){
  const n=now?new Date(now):new Date();
  const s=new Date(n.getFullYear(),n.getMonth(),n.getDate());
  s.setDate(s.getDate()-((n.getDay()+6)%7));
  return s.getTime();
}
/* The overview's live partial is pinned to the task row it belongs to, so
   stale or cross-run data can never land on a new task. The selected
   session keeps preferring the fresher event-sourced liveUsage. */
function livePartial(sid,tk){
  if(sid===selected&&liveUsage[sid])return liveUsage[sid];
  const l=liveAll[sid];
  return tk&&l&&l.task_id===tk.task_id?l.consumed:null;
}
/* v===1 && quality==="exact" && !partial — the only snapshot proven to
   post-date the double-count fix, whether it is a finished run_usage row
   or a live partial from the batch. */
const exactUsage=u=>!!u&&u.v===1&&u.quality==="exact"&&!u.partial;
function weekContrib(tk){
  const ru=tk.run_usage;
  if(ru&&typeof ru==="object"&&Object.keys(ru).length){
    const info=runTok(ru);
    return{total:info&&Number.isFinite(info.total)?info.total:0,
      live:false,approx:!exactUsage(ru)};
  }
  if(tk.status==="running"||tk.status==="queued"){
    const l=livePartial(tk.session_id,tk);
    const info=l&&runTok(l);
    if(info&&Number.isFinite(info.total))
      return{total:info.total,live:true,approx:!exactUsage(l)};
  }
  if(tk.usage&&typeof tk.usage==="object"){
    const m=tokCount(tk.usage);
    if(m!==null&&Number.isFinite(m))return{total:m,live:false,approx:true};
  }
  return{total:0,live:false,approx:false};
}
function weekStats(){
  const wk=weekStartMs();
  const byId=new Map();
  for(const tk of tasks){
    if(!tk||typeof tk!=="object")continue;
    const id=tk.task_id==null?tk:String(tk.task_id);
    const cur=byId.get(id);
    const hasRU=o=>o.run_usage&&typeof o.run_usage==="object"&&
      Object.keys(o.run_usage).length>0;
    if(!cur||(!hasRU(cur)&&hasRU(tk)))byId.set(id,tk);
  }
  const out={total:0,n:0,excluded:0,approx:false,live:false,start:wk};
  for(const tk of byId.values()){
    let ts=Date.parse(tk.started_at);
    if(!Number.isFinite(ts))ts=Date.parse(tk.created_at);
    if(!Number.isFinite(ts)){out.excluded++;continue}
    if(ts<wk)continue;
    out.n++;
    const c=weekContrib(tk);
    if(c.total>0){
      out.total+=c.total;
      if(c.live)out.live=true;
      if(c.approx)out.approx=true;
    }
  }
  return out;
}
/* Recomputed on overview polls, live usage updates and locale re-renders —
   pure recompute from tasks/liveUsage/liveAll, so a week rollover or a
   timezone change self-corrects on the next poll with no extra timer. The
   DOM writes are gated so stable polls never churn the node. */
let lastWeekVal="",lastWeekTitle="";
function renderWeek(){
  const el=$("#weektotal");if(!el)return;
  const valEl=el.querySelector(".wt-val");if(!valEl)return;
  const w=weekStats();
  const txt=(w.approx?"~":"")+fmtTok(w.total)+" "+t("tokens.unit");
  if(txt!==lastWeekVal){lastWeekVal=txt;valEl.textContent=txt}
  let dateStr="";
  try{dateStr=new Date(w.start).toLocaleDateString(locale,
    {weekday:"short",month:"short",day:"numeric"})}catch(e){
    try{dateStr=new Date(w.start).toLocaleDateString()}catch(e2){}}
  const parts=[t("tokens.week_title",{date:dateStr,n:w.n}),
    t("tokens.week_scope")];          // retained-history scope is always stated
  if(w.approx)parts.push(t("tokens.week_approx"));
  if(w.live)parts.push(t("tokens.week_live"));
  if(w.excluded)parts.push(t("tokens.week_excluded",{n:w.excluded}));
  const title=parts.join(" · ");
  if(title!==lastWeekTitle){lastWeekTitle=title;
    el.title=title;el.setAttribute("aria-label",title)}
}

/* ---------- sidebar ---------- */
let lastSidebarSig="";
function renderSidebar(){
  const el=$("#sesslist");
  const order={busy:0,spawning:1,ready:1,idle_unloaded:2,dead:3};
  const sorted=[...sessions].sort((a,b)=>(order[a.proc_state]??4)-(order[b.proc_state]??4)
    || new Date(b.last_active_at)-new Date(a.last_active_at));
  const sig=(selected||"")+"|"+sorted.map(s=>{
    const tk=latestTask(s.session_id);
    return [s.session_id,s.proc_state,s.last_active_at,s.title,s.agent,s.cwd,
      tk?tk.task_id:"",tk?tk.status:"",tk?tk.message:"",tk?tk.started_at:"",
      tk?tk.finished_at:"",tk?tk.created_at:"",tk?JSON.stringify(tk.run_usage||tk.usage||0):"",
      JSON.stringify(liveUsage[s.session_id]||0),
      JSON.stringify(liveAll[s.session_id]||0)].join(" ");
  }).join("|");
  if(sig===lastSidebarSig)return;
  lastSidebarSig=sig;
  const focusId=document.activeElement&&document.activeElement.dataset
    ?document.activeElement.dataset.id:null;
  el.innerHTML=sorted.map(s=>{
    const st=statusOf(s);
    const tk=latestTask(s.session_id);
    const sub=tk&&tk.message?tk.message.split("\n")[0].slice(0,80):baseName(s.cwd);
    const sel=s.session_id===selected;
    return `<button type="button" role="option" class="sess ${sel?"sel":""}" data-id="${esc(s.session_id)}"
        aria-selected="${sel}"${sel?' aria-current="true"':""} title="${esc(s.title||s.session_id)}">
      <span class="sicon">${agentAvatar(s,18)}</span>
      <span class="smeta">
        <span class="stitle">${esc(s.title||s.session_id)}</span>
        <span class="sstatus"><span class="sgr glyph--${st.tone}">${statusGlyph(st.tone)}</span><span${st.raw?` title="${esc(st.raw)}"`:""}>${esc(t(st.key))}</span>${durSpan(tk,"sdur")}${tokSpan(tk,"stok",livePartial(s.session_id,tk))}</span>
        ${sub?`<span class="ssub">${esc(sub)}</span>`:""}
      </span>
    </button>`}).join("")||'<div class="empty" style="margin-top:40px">'+esc(t("empty.no_sessions"))+'</div>';
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
  if(!s){el.innerHTML='<div class="hplaceholder">'+esc(t("session.select"))+'</div>';return}
  const st=statusOf(s);
  const tk=latestTask(s.session_id);
  const repo=baseName(s.cwd);
  const agoStr=s.last_active_at?ago(s.last_active_at):"";
  /* The header still names the displayed sub-agent's ids, now as the raw
     pair on its own line: both resolve fresh from this session's own rows
     on every render, and a missing value drops out instead of leaving
     stale text behind. */
  const taskId=tk&&tk.task_id!=null?String(tk.task_id):"";
  const ids=[taskId,s.session_id?String(s.session_id):""]
    .filter(Boolean).join(" / ");
  const agentModel=[s.agent,s.model].filter(Boolean).join("/");
  const info=[repo?`<span class="hrepo" title="${esc(s.cwd||"")}">${esc(repo)}</span>`:"",
    agentModel?`<span class="hagent">${esc(agentModel)}</span>`:""]
    .filter(Boolean).join('<span class="hsep">·</span>');
  /* Task controls gate on the latest applicable task: pause/cancel for an
     in-flight turn (a remote-owned row routes the request to its bridge),
     resume when the server's resumable gate says a continuation can land.
     While one req_* is in flight all three park — the pending reason lives
     on the disabled title. Icon-only in a 2x2 cluster: pause/resume on the
     title row, cancel/download on the status row. */
  const can=taskActions(tk,s),busy=actPending(s.session_id);
  const hbtns=["pause","resume","cancel"].map(a=>{
    const on=!busy&&can[a];
    return actBtn(a,on,on?t("act."+a)
      :busy?t("act.pending"):a==="resume"?t("act.no_resumable"):t("act.no_active"));
  }).join("");
  /* The transcript download fills the fourth grid cell inside the same
     role=group — gated on the session's cached events, not on any task
     state. */
  const dlOn=hasTranscript(s.session_id);
  const dlBtn=`<button type="button" class="hact" data-act="download" id="dlbtn"${dlOn?"":" disabled"} title="${esc(t(dlOn?"act.download_hint":"act.no_transcript"))}" aria-label="${esc(t("act.download"))}">${icon("download",14)}</button>`;
  el.innerHTML=`<div class="hgrid">
      <h2 class="htitle"><span class="sgr glyph--${st.tone}">${statusGlyph(st.tone,15)}</span><span class="htext">${esc(s.title||s.session_id)}</span></h2>
      <div class="hstatus"><span${st.raw?` title="${esc(st.raw)}"`:""}>${esc(t(st.key))}</span>${durSpan(tk,"hdur")}${tokSpan(tk,"htok",livePartial(s.session_id,tk))}${tpsSpan(tk,"htps",livePartial(s.session_id,tk))}</div>
      ${info?`<div class="hinfo">${info}</div>`:""}
      ${ids?`<div class="hids" title="${esc(ids)}">${esc(ids)}</div>`:""}
      <div class="hactions" role="group" aria-label="${esc(t("a11y.task_actions"))}">${hbtns}${dlBtn}</div>
      <span class="hturns">${esc(t("session.turns",{n:s.turns??0}))}</span>
      ${agoStr?`<span class="hage">${esc(agoStr)}</span>`:""}
    </div>`;
  el.querySelectorAll(".hact").forEach(b=>b.onclick=()=>
    b.dataset.act==="download"?downloadTranscript():taskAction(b.dataset.act));
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

/* Whitespace-split word counts mislead for space-less scripts: when the text
   is predominantly CJK the fold label counts characters instead. English and
   other space-separated text keep the plain word count. */
const CJK_RE=/[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]/g;
function thinkLabel(s){
  const words=s.split(/\s+/).filter(Boolean).length;
  const cjk=(s.match(CJK_RE)||[]).length;
  return cjk>words?t("transcript.thinking_chars",{n:(s.match(/\S/g)||[]).length})
    :t("transcript.thinking_words",{n:words});
}
function flushBlocks(){
  for(const b of dirty){
    if(b.tagName==="DETAILS"){
      b.querySelector(".body").textContent=b._text;
      b.querySelector(".tlabel").textContent=thinkLabel(b._text);
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
  d.innerHTML=`<div class="chead">${icon(user?"msg":"doc",15)}<span>${esc(t(user?"transcript.user_message":"transcript.dispatched_message"))}</span><span class="ctime">${esc(fmtTs(e.ts))}</span></div>
    <pre class="ptext ${long?"clamp":""}">${esc(e.text)}</pre>`;
  if(long){const x=document.createElement("button");x.type="button";x.className="expand";x.textContent=t("transcript.show_more");
    x.onclick=()=>{d.querySelector("pre").classList.remove("clamp");x.remove()};d.appendChild(x)}
  content.appendChild(d);
}
function msgBlock(ts){
  if(!curMsg){curMsg=document.createElement("div");curMsg.className="block card msg";
    curMsg.innerHTML=`<div class="chead">${icon("msg",15)}<span>${esc(t("transcript.agent"))}</span><span class="ctime">${esc(fmtTs(ts))}</span></div><div class="msg-body"></div>`;
    content.appendChild(curMsg);curMsg._text=""}
  curThink=null;curToolGroup=null;
  return curMsg;
}
function addMsg(e){
  const b=msgBlock(e.ts);b._text+=e.text;dirty.add(b);
}
function addThink(e){
  if(!curThink){curThink=document.createElement("details");curThink.className="block think";
    curThink.innerHTML=`<summary>${icon("sparkle",14)}<span class="tlabel">${esc(t("transcript.thinking"))}</span><span class="chev">${icon("chevron",13)}</span></summary><div class="body"></div>`;
    content.appendChild(curThink);curThink._text=""}
  curMsg=null;curToolGroup=null;
  curThink._text+=e.text;dirty.add(curThink);
}
const TOOL_ICON={execute:"monitor",read:"doc",search:"search",edit:"edit",fetch:"external"};
const TOOL_KINDS={tool:1,execute:1,read:1,search:1,edit:1,fetch:1};
/* Known tool kinds localize; anything else stays an escaped technical value. */
function toolKindLabel(kind){
  return TOOL_KINDS[kind]?t("tool.kind."+kind)
    :String(kind||"").charAt(0).toUpperCase()+String(kind||"").slice(1);
}
const TOOL_STATES={in_progress:1,completed:1,failed:1,error:1};
function addTool(e){
  flushBlocks();
  const kind=String(e.kind||"tool").toLowerCase();
  const prev=e.id?tools[e.id]:null;
  if(prev){
    /* A second full record for the same call (a legacy agy DONE step that
       normalized to a complete row) folds into the existing row instead of
       painting a duplicate. */
    if(e.title){const tt=prev.el.querySelector(".ttitle");tt.textContent=e.title;tt.title=e.input||""}
    if(e.status)addToolStatus({t:"tool_status",id:e.id,status:e.status,ts:e.ts});
    return;
  }
  const g=toolGroup();curMsg=null;curThink=null;
  const row=document.createElement("div");row.className="tool";
  row.innerHTML=`<span class="ticon">${icon(TOOL_ICON[kind]||"gear",15)}</span>
    <span class="tmain"><span class="tcap">${esc(toolKindLabel(kind))}</span>
      <span class="ttitle" title="${esc(e.input||"")}">${esc(e.title||e.id)}</span></span>
    <span class="tright"><span class="dur"></span><span class="st in_progress" role="img" aria-label="${esc(t("tool.status.in_progress"))}">${spin()}</span></span>`;
  g.appendChild(row);
  const rec={el:row,start:e.ts,status:"in_progress"};
  tools[e.id]=rec;
  if(e.input){const det=document.createElement("details");det.className="tooldetail";
    det.innerHTML=`<summary><span class="chev">${icon("chevron",11)}</span>${esc(t("tool.input"))}</summary><pre>${esc(e.input)}</pre>`;
    row.after(det);
    row.style.cursor="pointer";row.onclick=()=>det.open=!det.open;}
  /* A record that already carries a terminal status (legacy DONE step whose
     ACTIVE twin was truncated/never seen) closes immediately — no ts, so no
     invented 0ms duration. */
  if(e.status)addToolStatus({t:"tool_status",id:e.id,status:e.status});
}
function addToolStatus(e){
  const r=tools[e.id];if(!r)return;
  r.status=e.status;
  const st=r.el.querySelector(".st");
  st.className="st "+e.status;
  // Icons alone are ambiguous: known states get a localized accessible name;
  // unknown wire values stay raw text (diagnostic, not chrome).
  if(TOOL_STATES[e.status]){
    st.setAttribute("role","img");
    st.setAttribute("aria-label",t("tool.status."+e.status));
  }else{st.removeAttribute("role");st.removeAttribute("aria-label")}
  st.innerHTML=e.status==="in_progress"?spin()
    :e.status==="completed"?icon("checkCircle",15)
    :(e.status==="failed"||e.status==="error")?icon("xCircle",15)
    :esc(e.status);
  if(e.status!=="in_progress"&&r.start){
    const s=(new Date(e.ts)-new Date(r.start))/1000;
    if(Number.isFinite(s))
      r.el.querySelector(".dur").textContent=s>=1?t("time.dur_sec",{n:s.toFixed(1)}):t("time.dur_ms",{n:Math.round(s*1000)});
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
/* Known stop reasons localize; provider-specific values stay raw. */
const STOP_REASONS={stalled:1,cancelled:1,paused:1,error:1};
function stopReasonLabel(r){return STOP_REASONS[r]?t("stop."+r):String(r)}
function addTurn(e){
  closeBlocks();
  const d=document.createElement("div");d.className="turnend";
  const ms=turnDurMs(e,lastPromptTs);
  // Visible text: "turn ended · <duration>" — plus the stop reason only when
  // it is not end_turn. The wall-clock timestamp (and any error detail) live
  // in the tooltip, not the text.
  const parts=[t("transcript.turn_ended")];
  const dur=ms===null?"":fmtDur(ms);
  if(dur)parts.push(dur);
  if(e.stop_reason&&e.stop_reason!=="end_turn")parts.push(stopReasonLabel(e.stop_reason));
  const tip=[fmtTs(e.ts),e.error].filter(Boolean).join(" · ");
  if(tip)d.title=tip;
  d.innerHTML=`${icon("dot",10)}<span>${esc(parts.join(" · "))}</span>`;
  content.appendChild(d);
}
function addErr(e){
  closeBlocks();
  const d=document.createElement("div");d.className="turnend err";
  const parts=[e.text||t("transcript.error"),fmtTs(e.ts)].filter(Boolean);
  d.innerHTML=`${icon("xCircle",10)}<span>${esc(parts.join(" · "))}</span>`;
  content.appendChild(d);
}
const handlers={prompt:addPrompt,msg:addMsg,think:addThink,tool:addTool,
  tool_status:addToolStatus,turn:addTurn,error:addErr};

function applyEvents(evs){
  const nearBottom=content.scrollHeight-content.scrollTop-content.clientHeight<120;
  for(const e of evs)(handlers[e.t]||(()=>{}))(e);
  flushBlocks();
  if(nearBottom)content.scrollTop=content.scrollHeight;
}

/* ---------- transcript download ----------
   Exports the selected session's cached events as a Markdown file — the DOM
   is never consulted and no network round-trip happens. msg/think chunks
   coalesce into single blocks (the same merge the live pane applies),
   tool_status records fold into their tool line, and usage snapshots stay
   internal. Fences size dynamically so content can never break out of one,
   and every interpolated value is flattened to one line so metadata cannot
   inject fake headings or fences of its own. */
function hasTranscript(id){
  return !!id&&(eventsCache[id]||[]).some(e=>e&&handlers[e.t]);
}
function updateDlBtn(){
  const b=$("#dlbtn");if(!b)return;
  const on=hasTranscript(selected);
  b.disabled=!on;
  b.title=t(on?"act.download_hint":"act.no_transcript");
}
function sanitizeFilename(raw){
  /* Windows-forbidden < > : " / \ | ? * plus ';' (explicit requirement) and
     the C0/DEL control characters all become separators; separator runs
     collapse, unsafe edge characters strip, and the result caps at 100
     chars so the MM-DD-YYYY- prefix + .md stay well under MAX_PATH. */
  const c=String(raw??"")
    .replace(/[<>:"/\\|?*;\x00-\x1f\x7f]/g,"-")
    .replace(/[-_ ]+/g,"-")
    .replace(/^[-. ]+|[-. ]+$/g,"");
  return (c.length>100?c.slice(0,100).replace(/[-. ]+$/,""):c)||"transcription";
}
function buildTranscriptFilename(s){
  /* Session creation date in the browser's local timezone (the same zone
     fmtTs renders in); a missing/unparseable stamp falls back to today. */
  let d=s&&s.created_at?new Date(s.created_at):null;
  if(!d||!Number.isFinite(+d))d=new Date();
  const p=n=>String(n).padStart(2,"0");
  return `${p(d.getMonth()+1)}-${p(d.getDate())}-${d.getFullYear()}-`+
    sanitizeFilename(s&&(s.title||s.session_id))+".md";
}
const mdLine=v=>String(v??"").replace(/[\r\n]+/g," ").trim();
const mdCode=v=>{const s=mdLine(v);return s.includes("`")?"`` "+s+" ``":"`"+s+"`"};
/* A fence one char longer than the longest backtick run in the text can
   never be closed early by the content itself. */
function mdFence(text){
  const m=String(text).match(/`{3,}/g);
  return "`".repeat(m?Math.max(...m.map(r=>r.length))+1:3);
}
function sessionToMarkdown(s,evs){
  const L=[];
  const title=s?mdLine(s.title||s.session_id):"";
  L.push("# "+(title||t("dl.default_title")),"");
  if(s){
    const tk=latestTask(s.session_id);
    const repo=baseName(s.cwd);
    const meta=[[s.agent,s.model].filter(Boolean).join(" · "),
      [tk&&tk.task_id!=null?t("session.task_id",{id:String(tk.task_id)}):"",
        s.session_id?t("session.session_id",{id:s.session_id}):""]
        .filter(Boolean).join(" · "),
      repo?t("session.working_repo",{repo}):"",s.created_at||""];
    for(const line of meta){const v=mdLine(line);if(v)L.push("- "+v)}
    L.push("","---","");
  }
  let msg="",think="";
  const toolsMd={};   // tool_call_id -> {idx,kind,title,id,status}
  const tstat=st=>TOOL_STATES[st]?t("tool.status."+st):mdLine(st);
  const tline=r=>{
    let x="**"+r.kind+"**";
    const cap=mdLine(r.title||r.id||"");
    if(cap)x+=" — "+mdCode(cap);
    if(r.status)x+=" · "+tstat(r.status);
    return x;
  };
  const flushMsg=()=>{const v=msg.trim();msg="";
    if(v)L.push("## "+t("transcript.agent"),"",v,"")};
  const flushThink=()=>{const v=think.trim();think="";
    if(v)L.push("<details>","<summary>"+t("transcript.thinking")+"</summary>",
      "",v,"","</details>","")};
  const fence=text=>{const f=mdFence(text);L.push(f,String(text),f,"")};
  for(const e of evs||[]){
    if(!e||typeof e!=="object")continue;
    if(e.t==="msg"){flushThink();msg+=String(e.text??"");continue}
    if(e.t==="think"){flushMsg();think+=String(e.text??"");continue}
    flushMsg();flushThink();
    if(e.t==="prompt"){
      const user=e.src==="dashboard";
      L.push("## "+t(user?"transcript.user_message":"transcript.dispatched_message")+
        (e.ts?" ("+mdLine(e.ts)+")":""),"");
      fence(e.text??"");
    }else if(e.t==="tool"){
      const prev=e.id?toolsMd[e.id]:null;
      if(prev){          // a second record for the same call folds in
        if(e.title)prev.title=e.title;
        if(e.status)prev.status=e.status;
        L[prev.idx]=tline(prev);
      }else{
        const kind=String(e.kind||"tool").toLowerCase();
        const rec={kind:TOOL_KINDS[kind]?t("tool.kind."+kind)
            :mdLine(kind)||"tool",
          title:e.title||"",id:e.id||"",status:e.status||"",idx:L.length};
        L.push(tline(rec),"");
        if(e.id)toolsMd[e.id]=rec;
      }
      if(e.input)fence(e.input);
      if(e.output)fence(e.output);
    }else if(e.t==="tool_status"){
      const rec=e.id?toolsMd[e.id]:null;
      if(rec&&e.status){rec.status=e.status;L[rec.idx]=tline(rec)}
    }else if(e.t==="turn"){
      let line=t("transcript.turn_ended");
      if(e.stop_reason&&e.stop_reason!=="end_turn")
        line+=" · "+mdLine(stopReasonLabel(e.stop_reason));
      L.push("*"+line+"*","");
    }else if(e.t==="error"){
      L.push("> **"+t("transcript.error")+":** "+mdLine(e.text),"");
    }
  }
  flushMsg();flushThink();
  return L.join("\n").replace(/\n{3,}/g,"\n\n");
}
function downloadTranscript(){
  const id=selected;if(!id||!hasTranscript(id))return;
  const s=sessions.find(x=>x.session_id===id)||{session_id:id};
  const blob=new Blob([sessionToMarkdown(s,eventsCache[id])],
    {type:"text/markdown;charset=utf-8"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url;a.download=buildTranscriptFilename(s);a.style.display="none";
  document.body.appendChild(a);a.click();
  setTimeout(()=>{a.remove();URL.revokeObjectURL(url)},0);
}

/* ---------- conversation nav rail ----------
   Voyager ruler-mode timeline: one fixed-length tick per markable beat,
   packed as an evenly-spaced column centered on the rail's midpoint —
   positions are compact, not document fractions, so scrolling moves only
   the focus wave through the static group. The rail overlays the content
   viewport immediately left of the native scrollbar. The mark list is
   re-derived from the live DOM on each rAF-coalesced layout pass, so
   incremental appends, chunked replay, transcript resets, pane
   stash/restore, fold toggles, prompt expansion and resizes all self-heal
   with no bookkeeping in the render path. */
const rail=$("#rail"),pane=$("#pane");
/* Mark taxonomy — the single source of truth, applied by layoutRail via
   querySelectorAll and consumed verbatim by the tests and benchmark: Agent
   message cards, MCP-dispatched prompt cards and successful turn ends.
   Dashboard-authored "User Message" cards (.prompt.user), thinking folds,
   tool groups, error dividers (.turnend.err — an error is not a turn end),
   empty states and usage events never get a mark. */
const MARK_SEL=".block.card.msg,.block.card.prompt:not(.user),.turnend:not(.err)";
/* Kind is encoded by thickness only — every tick keeps the same fixed 14px
   length: agent message 2px, dispatched message 3px, turn end 4px. */
const MARK_CLS=["k-msg","k-disp","k-turn"],MARK_H=[2,3,4];
const MARK_LBL=["rail.agent_message","rail.dispatched_message","rail.turn_end"];
let railQueued=false,railBtns=[],curIdx=-1;
const rmo=matchMedia("(prefers-reduced-motion: reduce)");

/* Tick position (--my) and wave emphasis (--mo) ride CSS custom properties
   so hover/current overrides stay declarative. Probe once whether var()
   resolves inside transform; where it cannot, the write paths below use
   literal transform/opacity instead and the CSS state overrides degrade to
   whatever the JS last wrote. */
let markVars=false;
try{
  const p=document.createElement("button");
  p.className="mark";p.style.setProperty("--my","7px");rail.appendChild(p);
  const tr=window.getComputedStyle?getComputedStyle(p).transform:"";
  markVars=!!tr&&tr!=="none";p.remove();
}catch(e){}

function scheduleRail(){
  if(railQueued)return;railQueued=true;
  requestAnimationFrame(()=>{railQueued=false;layoutRail()});
}

/* Mark thickness rank by card kind: agent message 0 (2px), dispatched
   prompt 1 (3px), turn end 2 (4px). */
function markRank(el){
  return el.classList.contains("turnend")?2
    :el.classList.contains("prompt")?1:0;
}

/* Ruler geometry (Voyager buildCompactMarkerOffsets): one tick per markable
   element, evenly spaced in a compact column centered on the rail's
   midpoint — never a document-fraction mapping and never clustered, so
   every source keeps its own tick and label. step = min(8px, 160px/(n-1))
   caps the whole group near 160px; clamping the center keeps every tick
   fully inside the rail. The scrollbar gutter is measured here too
   (offsetWidth-clientWidth) and exposed as --railin, so the rail hugs the
   native scrollbar's left edge while header/composer reserve the same
   strip — one shared center axis. */
function layoutRail(){
  const sw=Math.max(0,(content.offsetWidth||0)-content.clientWidth);
  pane.style.setProperty("--railin",sw+"px");
  const els=[...content.querySelectorAll(MARK_SEL)];
  if(!selected||!els.length){
    rail.hidden=true;railBtns=[];curIdx=-1;
    if(rail.replaceChildren)rail.replaceChildren();else rail.innerHTML="";
    return;
  }
  rail.hidden=false;
  const RH=rail.clientHeight,n=els.length;
  const step=Math.min(8,160/Math.max(1,n-1)),mid=RH/2;
  while(railBtns.length>n)railBtns.pop().remove();
  els.forEach((el,i)=>{
    let b=railBtns[i];
    if(!b){b=document.createElement("button");b.type="button";b.className="mark";
      // Keyboard-activated clicks (detail 0) also move focus to the card.
      b.onclick=e=>jumpToMark(b,e&&e.detail===0);
      rail.appendChild(b);railBtns.push(b)}
    b._els=[el];
    const rank=markRank(el);
    b.className="mark "+MARK_CLS[rank];
    const th=MARK_H[rank];
    const c=Math.max(th/2,Math.min(mid+(i-(n-1)/2)*step,RH-th/2));
    b.style.setProperty("--my",c.toFixed(1)+"px");
    if(!markVars)
      b.style.transform=`translateY(${c.toFixed(1)}px) translateY(-50%)`;
    const cts=(el.querySelector(".ctime")||{}).textContent||"";
    const lbl=[t(MARK_LBL[rank]),cts].filter(Boolean).join(" · ");
    b.setAttribute("aria-label",lbl);b.title=lbl;
  });
  curIdx=-2;                    // force the aria-current/tab-stop sync below
  updateCurMark();
  updateRulerWave();
}

function jumpToMark(b,toCard){
  const el=b._els&&b._els[0];if(!el||!el.isConnected)return;
  const top=Math.max(0,el.offsetTop-8);
  if(content.scrollTo)content.scrollTo({top,behavior:rmo.matches?"auto":"smooth"});
  else content.scrollTop=top;
  if(toCard){el.tabIndex=-1;if(el.focus)el.focus({preventScroll:true})}
}

function updateCurMark(){
  /* Voyager's 0.45 reference: the current beat is the last mark whose card
     top sits above 45% of the viewport — the same line the wave crests on,
     so the accent mark always rides the crest. */
  const y=content.scrollTop+content.clientHeight*.45;let idx=-1;
  for(let i=0;i<railBtns.length;i++){
    const el=railBtns[i]._els&&railBtns[i]._els[0];
    if(el&&el.isConnected&&el.offsetTop<=y)idx=i;else break;   // tops are monotonic
  }
  /* Nothing above the reference line yet (the first card sits below it):
     the first mark is current — the same rest position the tab stop uses. */
  if(idx<0&&railBtns.length)idx=0;
  if(idx===curIdx)return;
  curIdx=idx;
  // Exactly one tabbable mark: the focused one while the rail is in use,
  // otherwise the current mark (or the first when nothing is current yet).
  const focused=railBtns.indexOf(document.activeElement);
  const tab=focused>=0?focused:(idx<0?0:idx);
  railBtns.forEach((b,i)=>{
    b.classList.toggle("cur",i===idx);
    if(i===idx)b.setAttribute("aria-current","true");else b.removeAttribute("aria-current");
    b.tabIndex=i===tab?0:-1;
  });
}

/* The Voyager focus wave: a Gaussian crest of brightened ticks tracks the
   0.45 viewport reference while scrolling. Opacity is the only channel —
   every tick keeps its fixed length and rail position, so the effect is
   pure compositor work with zero layout thrash. */
const WAVE_SIGMA=1.2;
function updateRulerWave(){
  const n=railBtns.length;if(!n)return;
  // Fractional index: interpolate inside the bracketing mark anchors.
  const tops=[];let last=0;
  for(const b of railBtns){const el=b._els&&b._els[0];
    last=el&&el.isConnected?el.offsetTop:last;tops.push(last)}
  const focus=content.scrollTop+content.clientHeight*.45;
  let fi=0;
  if(focus>=tops[n-1])fi=n-1;
  else if(focus>tops[0]){
    let lo=0,hi=n-1;
    while(hi-lo>1){const m=(lo+hi)>>1;if(tops[m]<=focus)lo=m;else hi=m}
    fi=lo+(focus-tops[lo])/Math.max(1,tops[hi]-tops[lo]);
  }
  railBtns.forEach((b,i)=>{
    const d=i-fi,crest=Math.exp(-d*d/(2*WAVE_SIGMA*WAVE_SIGMA));
    const mo=(i===curIdx?1:.42+.50*crest).toFixed(3);
    b.style.setProperty("--mo",mo);
    if(!markVars)b.style.opacity=mo;
  });
}

/* Roving tabindex: one tab stop total; arrows/Home/End move it, Enter/Space
   activate natively (button click -> jumpToMark). */
rail.addEventListener("keydown",e=>{
  const i=railBtns.indexOf(document.activeElement);
  if(i<0)return;
  let n;
  if(e.key==="ArrowDown")n=Math.min(i+1,railBtns.length-1);
  else if(e.key==="ArrowUp")n=Math.max(i-1,0);
  else if(e.key==="Home")n=0;
  else if(e.key==="End")n=railBtns.length-1;
  else return;
  e.preventDefault();
  if(n===i)return;
  railBtns[n].tabIndex=0;railBtns[i].tabIndex=-1;
  railBtns[n].focus();
});
/* Wheeling over the rail scrolls the conversation (Voyager parity). */
rail.addEventListener("wheel",e=>{
  if(!e.deltaY)return;
  content.scrollTop+=e.deltaY;e.preventDefault();
},{passive:false});

/* Any DOM/geometry change re-derives the marks next frame. Feature-detected:
   without them the rail simply shows the marks computed at select() time. */
if(window.MutationObserver)
  new MutationObserver(scheduleRail).observe(content,
    {childList:true,subtree:true,characterData:true,
     attributes:true,attributeFilter:["open","class"]});
if(window.ResizeObserver)new ResizeObserver(scheduleRail).observe(content);

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
/* Whether /api/events has answered at least once for a session — lets an
   empty transcript distinguish "Loading…" from "No events" after a locale
   re-render. */
const polled={};
function dropCache(id){
  delete eventsCache[id];delete offsets[id];delete rendered[id];delete replayGen[id];
  delete liveUsage[id];delete polled[id];
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
    polled[id]=true;
    if(j.reset)eventsCache[id]=[];
    eventsCache[id]=(eventsCache[id]||[]).concat(j.events);
    offsets[id]=j.offset;
    updateDlBtn();           // transcript readiness flips on any append/reset
    // "usage" events carry the live run-consumption snapshot; a prompt or
    // turn boundary clears it so a finished run never shows stale partials.
    let usageDirty=false;
    for(const e of j.events){
      if(e.t==="usage"&&e.consumed){liveUsage[id]=e.consumed;usageDirty=true}
      else if(e.t==="prompt"||e.t==="turn"){
        if(liveUsage[id]){delete liveUsage[id];usageDirty=true}
      }
    }
    if(usageDirty){renderSidebar();renderSessionHeader();renderWeek()}
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
    else if(!content.children.length)content.innerHTML='<div class="empty">'+esc(t("empty.no_events"))+'</div>';
  }catch(e){}
}

/* connecting|live|disconnected — re-rendered in place on locale changes. */
let liveState="connecting";
function renderLive(){
  const l=$("#live");
  l.className=liveState==="live"?"on":liveState==="disconnected"?"off":"";
  l.querySelector(".lt").textContent=t("status."+liveState);
}
function setLive(on){liveState=on?"live":"disconnected";renderLive()}

async function pollOverview(){
  try{
    const r=await fetch("/api/overview");if(!r.ok)return;
    const j=await r.json();sessions=j.sessions;tasks=j.tasks;
    // Wholesale replace: the server prunes finished sessions, so stale
    // partials vanish on the next poll without client-side cleanup.
    liveAll=j.live||{};
    outboxQ=j.outbox||{};
    const known=new Set(sessions.map(s=>s.session_id));
    for(const id of new Set([...Object.keys(eventsCache),...Object.keys(panes),
        ...Object.keys(offsets),...Object.keys(rendered)]))
      if(!known.has(id)&&id!==selected)dropCache(id);
    renderSidebar();renderSessionHeader();renderWeek();renderQueue();
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
const sendState={};  // session_id -> {name, key, params, detail, final}
const sendPolls={};  // outbox name -> polling loop already running
/* Status line keeps a dictionary key + params (never rendered text) so a
   locale switch re-renders it without replaying network calls. `detail`
   carries the untranslated raw diagnostic for the tooltip. */
function renderSendStatus(){
  const el=$("#chatstatus");
  const st=selected?sendState[selected]:null;
  el.textContent=st&&st.key?t(st.key,st.params||undefined):"";
  el.title=st&&st.detail||"";
}
function refreshComposer(){
  renderSendStatus();
  const s=selected&&sessions.find(x=>x.session_id===selected);
  chatInput.placeholder=s?t("chat.placeholder.session",{session:s.title||s.session_id})
    :selected?t("chat.placeholder.short"):t("chat.placeholder.default");
}
function sendKey(j){
  const st=j&&j.state;
  return st==="waiting_busy"?"send.waiting_busy"
    :st==="waiting_owner"?"send.waiting_owner"
    :st==="delivering"?"send.delivering"
    :"send.queued";
}
/* error_code -> dictionary key for known bridge-reported failures. Anything
   else falls back to the generic failure with the raw error as tooltip
   detail — not_found is routing-only (the page's fixed /api/* URLs can never
   receive it), so it intentionally stays on the generic label. */
const SEND_ERR={expired:"send.err_expired",
  unknown_session:"send.err_unknown_session",
  missing:"send.err_missing",
  empty_or_too_long:"send.err_empty_or_too_long",
  bad_session:"send.err_bad_session",
  bad_request:"send.err_bad_request",
  bad_name:"send.err_bad_request",
  bad_action:"send.err_bad_request",
  bad_task:"send.err_bad_request",
  invalid_record:"send.err_invalid_record",
  dispatch_failed:"send.err_dispatch_failed",
  dispatch_error:"send.err_dispatch_error",
  delivering:"queue.err_delivering",
  dispatched:"queue.err_dispatched",
  dequeue_failed:"queue.err_failed",
  unknown_task:"act.err_unknown_task",
  wrong_session:"act.err_wrong_session",
  pause_failed:"act.failed",
  cancel_failed:"act.failed",
  resume_failed:"act.failed"};
function sendErrState(j,req){
  /* req: a req_* task-action done record — its failures carry "code"
     (resume) or "error_code" (pause/cancel), and its wording is action-
     oriented, so the generic fallback is act.failed and an expired request
     never reads like an expired message. */
  const code=j&&(j.error_code||j.code);
  let k=code&&SEND_ERR[code];
  if(req)k=code==="expired"?"act.err_expired":k||"act.failed";
  return{key:k||"send.failed",detail:j&&j.error||"",final:true};
}
function setSendState(sid,st){
  const cur=sendState[sid]||(sendState[sid]={});
  cur.key=st.key;cur.params=st.params||null;cur.detail=st.detail||"";cur.final=!!st.final;
  if(selected===sid)renderSendStatus();
}
async function sendChat(){
  const ta=$("#chatinput"),text=ta.value.trim();
  if(!text||!selected)return;
  const sid=selected;
  ta.value="";ta.style.height="";setSendState(sid,{key:"send.sending"});
  try{
    const r=await fetch("/api/send",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:sid,text})});
    const j=await r.json();
    if(!j.ok){setSendState(sid,sendErrState(j));return}
    setSendState(sid,{key:"send.queued"});
    sendState[sid].name=j.name;
    pollSendStatus(j.name,sid);
  }catch(e){setSendState(sid,{key:"send.failed",final:true})}
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
      if(j&&j.pending){setSendState(sid,{key:sendKey(j)});continue}
      const done=j||{};
      if(/^req_/.test(name)){
        /* Task-control request: the done record's state names the outcome —
           paused/cancelled/resumed, or the task status the action landed on. */
        setSendState(sid,done.ok
          ?{key:ACT_DONE[done.state]||"status.proc.done",
            params:done.task_id?{task:done.task_id}:null,final:true}
          :sendErrState(done,true));
        renderSessionHeader();   // controls re-enable with the real outcome
        pollOverview();          // pull the new task state in promptly
        return;
      }
      setSendState(sid,done.ok
        ?{key:done.task_id?"send.dispatched_task":"send.dispatched",
          params:done.task_id?{task:done.task_id}:null,final:true}
        :sendErrState(done));
      return;
    }
  }finally{delete sendPolls[name]}
}

/* ---------- outbox queue + session task controls ----------
   Queued chat records live in outbox/ as msg_*.json until the bridge claims
   them, so the list above the composer is re-read from /api/overview each
   poll and survives reloads. Steer pulls a record back into the composer
   for editing; delete drops it — both are one atomic dequeue server-side,
   and a record the bridge claimed in between reports back as delivering.
   Session task controls drop req_{pause,cancel,resume}_*.json requests into
   the same outbox; whichever bridge owns the task row runs the normal
   pause/cancel/resume path and reports through the done/ channel this page
   already polls — the dashboard never mutates task state itself. */
let outboxQ={};          // session_id -> [{name,message,ts,state,attempts}]
let lastQueueSig="";
const ACT_ICON={pause:"pause",cancel:"xCircle",resume:"play"};
const ACT_DONE={paused:"act.paused",cancelled:"act.cancelled",
  resumed:"act.resumed",completed:"status.proc.done",
  failed:"status.proc.failed",running:"status.proc.running",
  queued:"send.queued"};
const actBusy={};        // session_id -> /api/task_action POST in flight
function taskActions(tk,s){
  const off={pause:false,cancel:false,resume:false};
  if(!tk||!s||s.proc_state==="dead")return off;
  /* Pause/cancel only on in-flight tasks (remote-owned ones included — the
     request routes to the owning bridge). Resume follows the server's
     resumable gate, minus rows a continuation already replaced. */
  const active=tk.status==="queued"||tk.status==="running";
  return{pause:active,cancel:active,
    resume:!!tk.resumable&&!tk.resumed_by};
}
function actPending(sid){
  const cur=sid&&sendState[sid];
  return!!(actBusy[sid]||cur&&cur.name&&/^req_/.test(cur.name)&&!cur.final);
}
function actBtn(act,on,why){
  const label=t("act."+act);
  return `<button type="button" class="hact" data-act="${act}"${on?"":" disabled"} title="${esc(why)}" aria-label="${esc(label)}">${icon(ACT_ICON[act],14)}</button>`;
}
async function taskAction(act){
  const sid=selected;if(!sid)return;
  const s=sessions.find(x=>x.session_id===sid);if(!s)return;
  const tk=latestTask(sid);if(!tk||tk.task_id==null)return;
  if(!taskActions(tk,s)[act]||actPending(sid))return;   // stale state guard
  actBusy[sid]=true;                 // parks all three controls from click 1
  setSendState(sid,{key:"act.requesting"});
  try{
    const r=await fetch("/api/task_action",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({task_id:tk.task_id,session_id:sid,action:act})});
    const j=await r.json();
    if(!j.ok){setSendState(sid,sendErrState(j,true));return}
    setSendState(sid,{key:"send.queued"});
    sendState[sid].name=j.name;
    renderSessionHeader();          // controls park while the request rides
    pollSendStatus(j.name,sid);
  }catch(e){setSendState(sid,{key:"act.failed",final:true})}
  finally{delete actBusy[sid];renderSessionHeader()}
}
function renderQueue(){
  const el=$("#queue");if(!el)return;
  const items=selected?(outboxQ[selected]||[]):[];
  const sig=(selected||"")+"|"+items.map(m=>
    [m.name,m.message,m.state,m.attempts,m.ts].join(" ")).join("|");
  if(sig===lastQueueSig)return;
  lastQueueSig=sig;
  el.innerHTML=items.map(m=>{
    const age=Number.isFinite(m.ts)?ago(new Date(m.ts*1000).toISOString()):"";
    const meta=[t(sendKey(m)),age].filter(Boolean).join(" · ");
    return `<div class="qcard" role="listitem" data-name="${esc(m.name)}">
      <span class="qicon">${icon("queueMsg",13)}</span>
      <span class="qtext" title="${esc(m.message)}">${esc(m.message)}</span>
      <span class="qmeta">${esc(meta)}</span>
      <button type="button" class="qact qsteer" data-act="steer" data-name="${esc(m.name)}" title="${esc(t("queue.steer_hint"))}" aria-label="${esc(t("queue.steer_hint"))}">${icon("steer",13)}<span>${esc(t("queue.steer"))}</span></button>
      <button type="button" class="qact qdel" data-act="delete" data-name="${esc(m.name)}" title="${esc(t("queue.delete"))}" aria-label="${esc(t("queue.delete"))}">${icon("trash",13)}</button>
    </div>`;
  }).join("");
  el.hidden=!items.length;
  el.querySelectorAll(".qact").forEach(b=>b.onclick=()=>
    b.dataset.act==="steer"?steerMsg(b.dataset.name):delMsg(b.dataset.name));
}
function dropQueueEntry(sid,name){
  const items=outboxQ[sid];
  if(items)outboxQ[sid]=items.filter(m=>m.name!==name);
  lastQueueSig="";
  renderQueue();
}
async function dequeueMsg(name){
  try{
    const r=await fetch("/api/dequeue",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name})});
    return await r.json();
  }catch(e){return{ok:false,error:String(e),error_code:"dequeue_failed"}}
}
async function steerMsg(name){
  const j=await dequeueMsg(name);
  const sid=(j&&j.session_id)||selected;
  if(!j||!j.ok){setSendState(sid||"",sendErrState(j));return}
  if(sid===selected){
    chatInput.value=String(j.message||"");
    chatInput.style.height="auto";
    chatInput.style.height=Math.min(chatInput.scrollHeight,160)+"px";
    chatInput.focus();
  }
  setSendState(sid,{key:"queue.recalled",final:true});
  dropQueueEntry(sid,name);
}
async function delMsg(name){
  const j=await dequeueMsg(name);
  const sid=(j&&j.session_id)||selected;
  if(!j||!j.ok){setSendState(sid||"",sendErrState(j));return}
  setSendState(sid,{key:"queue.deleted",final:true});
  dropQueueEntry(sid,name);
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

/* ---------- language (System · English · 简体中文 · 繁體中文) ----------
   Switching re-renders every localized surface from source data — the pane
   is rebuilt from eventsCache (no transcript refetch), stashed panes are
   dropped because their labels are locale-bound, and the send status line
   re-renders from its stored key/params. */
function rerenderLocale(){
  for(const id of[...paneLru])dropPane(id);
  for(const id of Object.keys(rendered))rendered[id]=0;
  if(replaying){replayGen[replaying]=(replayGen[replaying]||0)+1;replaying=null}
  const refocus=document.activeElement===$("#chatinput")||document.activeElement===$("#chatsend");
  closeBlocks();tools={};lastPromptTs=null;
  const id=selected;
  if(id&&eventsCache[id]&&eventsCache[id].length){
    const pin=content.scrollHeight-content.scrollTop-content.clientHeight<120;
    content.innerHTML="";
    startReplay(id,pin);
  }else{
    content.innerHTML='<div class="empty">'+esc(t(id
      ?(polled[id]?"empty.no_events":"empty.loading")
      :"session.select"))+'</div>';
  }
  if(refocus)$("#chatinput").focus();
  lastSidebarSig="";                 // labels are baked into the sidebar DOM
  lastQueueSig="";                   // so are the queue cards' state labels
  renderSidebar();renderSessionHeader();renderLive();refreshComposer();
  renderWeek();renderQueue();
  scheduleRail();
}
function applyLocale(){
  locale=langPref==="system"?resolveSystemLocale():langPref;
  document.documentElement.lang=locale;
  document.title=t("app.title");
  const sel=$("#langsel");if(sel&&sel.value!==langPref)sel.value=langPref;
  applyStatic();
  rerenderLocale();
}
function setLocalePref(p){
  langPref=LOCALE_PREFS.includes(p)?p:"system";
  try{localStorage.setItem(LOCALE_KEY,langPref)}catch(e){}
  applyLocale();
}
$("#langsel").addEventListener("change",e=>setLocalePref(e.target.value));
addEventListener("storage",e=>{if(e.key!==LOCALE_KEY)return;
  const p=localePref();if(p!==langPref){langPref=p;applyLocale()}});
// Only a "system" preference tracks OS/browser language changes.
addEventListener("languagechange",()=>{if(langPref==="system")applyLocale()});
applyLocale();

function select(id){
  if(selected!==id){
    const prev=selected,wasReplaying=replaying;
    selected=id;
    if(prev)replayGen[prev]=(replayGen[prev]||0)+1;   // abort the outgoing replay only
    replaying=null;
    chatInput.disabled=false;$("#chatsend").disabled=false;
    refreshComposer();
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
    }else{
      closeBlocks();tools={};lastPromptTs=null;rendered[id]=0;
      if(eventsCache[id]&&eventsCache[id].length){
        content.innerHTML="";
        startReplay(id,true);
      }else{
        eventsCache[id]=[];offsets[id]=0;
        content.innerHTML='<div class="empty">'+esc(t("empty.loading"))+'</div>';
      }
    }
    touchCache(id);
    pollEvents();
  }
  renderSidebar();renderSessionHeader();closeSidebar();renderQueue();
  scheduleRail();
}

content.addEventListener("scroll",()=>{updateCurMark();updateRulerWave()});

pollOverview();
ovTimer=setInterval(pollOverview,3000);
pollTimer=setInterval(pollEvents,1500);
setInterval(tickDurations,1000);
</script>
</body>
</html>
"""
