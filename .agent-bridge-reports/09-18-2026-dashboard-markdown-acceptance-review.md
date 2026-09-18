# Agent Bridge Dashboard Markdown Rendering Acceptance Review

**Document Version:** 1.0.0  
**Date:** September 18, 2026  
**Role:** Independent Read-Only Acceptance Reviewer  
**Target File:** `C:\Users\Touma\Documents\Agent-Bridge\.agent-bridge-reports\09-18-2026-dashboard-markdown-acceptance-review.md`  
**Active Worktree:** `C:\Users\Touma\Documents\Agent-Bridge` (branch `main`, baseline HEAD `15c3605257fec88cf92864f128473ce7ce0105f5`)  
**Scope:** Review and adversarial verification of uncommitted candidate changes for dashboard Markdown syntax rendering in:
- `src/agent_bridge/share/dashboard_page.py`
- `tests/dashboard_status_behavior.js`
- `tests/test_dashboard_page.py`

---

## 1. Executive Summary & Verdict

The user requirement requested proper Markdown rendering across all four timeline message kinds:
1. **Dispatched message** (`prompt` with `src !== "dashboard"`)
2. **User message** (`prompt` with `src === "dashboard"`)
3. **Agent message** (`msg` / `message_chunk`)
4. **Thinking** (`think` / `thought_chunk`)

Previously, only Agent messages had a primitive ~30-line regex heuristic renderer (`md()`) that discarded code fence languages, lacked links, lacked italics/strike/blockquotes/rules, and broke on nested lists. Dispatched and User messages rendered inside `<pre class="ptext">` plain-text containers, while Thinking rendered exclusively via `.textContent`.

The candidate implementation introduces a unified, self-contained, escape-first Markdown tokenizer and block-state parser in [`src/agent_bridge/share/dashboard_page.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py). It routes all four timeline text kinds through this single engine, applies unified typography styles via `.md`, hardens links with protocol allowlists and new-window attributes, and maintains strict zero-external-network CSP compatibility with no DOM thrashing during token streaming.

Every coordinator check and independent adversarial probe passed without regression.

**Verdict: ACCEPT**

---

## 2. Evaluation Against Adversarial Invariants

### Invariant 1: Single Renderer Architecture & Rich Formatting Fidelity
**Status: PASS**
- **Unified Routing**:
  - `addPrompt(e)` renders `<div class="ptext md${long ? " clamp" : ""}">${md(e.text)}</div>` for both Dispatched (`src !== "dashboard"`) and User (`src === "dashboard"`) messages ([`dashboard_page.py#L1592-L1595`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L1592-L1595)).
  - `flushBlocks()` renders `b.querySelector(".msg-body").innerHTML = md(b._text)` for Agent messages and `b.querySelector(".body").innerHTML = md(b._text)` for Thinking folds ([`dashboard_page.py#L1575-L1578`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L1575-L1578)).
  - Containers carry class `md`, inheriting unified styling for all blocks and inlines ([`dashboard_page.py#L251-L267`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L251-L267)).
- **Rich Elements Handled**:
  - Headings (`h1` through `h6`) with trailing `#` strip.
  - Paragraphs and line breaks: single newline becomes `<br>`, double newline becomes `<p>`.
  - Fenced code with language tags: captures and sanitizes language identifiers, emitting `<pre><code class="language-${lang}">`.
  - Inline code: `` `code` `` stashed safely away from inline formatting.
  - Emphasis: `***bold italic***`, `**bold**`, `__bold__`, `*italic*`, `_italic_` (with intraword protection for underscores), and `~~strike~~`.
  - Lists: Ordered (`ol`, preserving non-1 `start` attribute), unordered (`ul`), task lists (`- [ ]`, `- [x]`, `- [X]` rendered with disabled checkboxes), and multi-level nested lists tracking indent depths.
  - Blockquotes: Single and recursive nested blockquotes (`> > quote`).
  - Horizontal rules: `---`, `***`, `___`.
  - Links: Markdown links `[text](url)` validated against allowlisted protocols.

---

### Invariant 2: XSS Prevention & Attribute Hardening
**Status: PASS**
- **Escape-First Guarantee**:
  - The entire input string is stripped of NUL characters (`\x00`), normalized for carriage returns, and completely escaped via `esc()` before any block or inline parsing occurs:
    ```javascript
    blocks(esc(String(src).replace(/\x00/g,"").replace(/\r\n?/g,"\n")).split("\n"))
    ```
  - All `<`, `>`, `&`, `"`, `'` characters become HTML character entity references (`&lt;`, `&gt;`, `&amp;`, `&quot;`, `&#39;`).
  - As a result, no raw user or model input can ever introduce an unescaped HTML element.
- **Link Target & Scheme Neutralization**:
  - The URL allowlist is defined by `const MD_URL = /^(?:https?:\/\/|mailto:|\/|#)/i`.
  - Any URL failing this allowlist (e.g., `javascript:...`, `data:...`, `vbscript:...`) is neutralized into inert text and inline code:
    `${em(txt)} (<code>${url}</code>)`.
  - Valid links strictly emit hardened anchor tags:
    `<a href="${url}" target="_blank" rel="noopener noreferrer">${em(txt)}</a>`.
  - Attribute breakout via entity tricks (e.g., `&quot; onfocus=...`) is impossible because HTML5 attribute parsers do not change tokenizer states on entity references within quoted attribute values.
- **Fenced Code Class Hardening**:
  - Language tags are strictly validated against `/^[A-Za-z0-9_+.#-]{1,30}$/`. Unsanitary strings or injection payloads (e.g., spaces, quotes, `<script>`) reject the class attribute entirely, falling back to safe `<pre><code>`.
- **Stash/Placeholder Integrity**:
  - Stash placeholders use `\x00${index}\x00`. Because all `\x00` in the input are stripped up-front and indices increment monotonically, user input cannot forge or corrupt stash tokens. Unstashing operates via fixpoint loop and functional replacer `(m, i) => stash[+i]`, avoiding pattern substitution bugs.

---

### Invariant 3: Streaming Stability, Batch Flushing & Parser Complexity
**Status: PASS**
- **Zero Per-Chunk DOM Thrashing**:
  - `addMsg(e)` and `addThink(e)` only append raw chunks to `_text` and add the element to the `dirty` Set:
    ```javascript
    curMsg._text += e.text; dirty.add(curMsg);
    curThink._text += e.text; dirty.add(curThink);
    ```
  - DOM parsing and `innerHTML` assignments occur solely during `flushBlocks()`, called at turn end or batched poll slices.
  - Replay benchmark confirms `msgBodySets: 1` over 1,202 wire events.
- **Streaming Partial Markdown & Unclosed Fences**:
  - Character-by-character incremental streaming test over a full Markdown document (331 prefix states) confirmed 0 runtime exceptions.
  - Unclosed fences mid-stream (` ```js\nconsole.log(1) ` without trailing fence) smoothly render `<pre><code class="language-js">console.log(1)</code></pre>` without throwing or dropping tokens.
- **Linear Complexity & ReDoS Immunity**:
  - Pathological input probes (50,000 repeating emphasis markers, 50,000 backticks, 50,000 bracket tokens, 10,000 list items, 1,000 nested quotes) executed in under 2 seconds. Regular expressions operate line-by-line with bounded lookaheads.

---

### Invariant 4: Layout Controls, Accessibility & CSP Preservation
**Status: PASS**
- **Prompt Clamping & Show-More**:
  - Long prompts (>900 chars) in Dispatched and User cards apply `.clamp` to `.ptext` ([`dashboard_page.py#L1593-L1595`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L1593-L1595)).
  - The "Show more" button queries the stable class `.ptext` (`d.querySelector(".ptext").classList.remove("clamp")`), which works regardless of container tag changes.
- **Thinking Fold Behavior**:
  - Stays closed by default (`<details class="block think">` without `open` attribute).
  - Preserves character/word count summary labels via `thinkLabel(b._text)` on `.tlabel`.
  - Removed plaintext `white-space: pre-wrap` from `details.think .body` to avoid double-spaced gaps when rendering structured `<p>` and `<pre>` elements.
- **Strict CSP Intact**:
  - `src/agent_bridge/share/dashboard.py` CSP header (`default-src 'none'; script-src 'unsafe-inline'; ...`) is completely untouched. Zero external assets, scripts, or CDNs were introduced.
- **Transcript Markdown Export**:
  - Transcript download functions (`sessionToMarkdown`, `sanitizeFilename`, `mdLine`, `mdCode`, `mdFence`) remain byte-for-byte identical. All 24 transcript download behavioral tests pass.

---

### Invariant 5: Git Worktree Hygiene & Isolation
**Status: PASS**
- **Orthogonal to Pre-Existing Dirty Changes**:
  - Preserved the pre-existing presence heartbeat in [`dashboard_page.py#L475`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L475) (`ping(0);setInterval(()=>ping(0),15000);`).
  - Preserved pre-existing presence tests in [`tests/test_dashboard_page.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_page.py) (`test_presence_survives_throttled_heartbeat`, `PRESENCE_TTL == 180.0`).
  - No out-of-scope files or adapters were touched.
  - No repository files were modified or created during review.

---

### Invariant 6: Test Suite Rigor & Behavioral Coverage
**Status: PASS**
- **`tests/dashboard_status_behavior.js`**:
  - Tests actual rendered DOM structures and CSS class states in Node's VM context:
    - Block wrapping (`<p>`, `<br>`, `<hr>`, headings, blockquotes, lists, task checkboxes).
    - Inline formatting (bold, italic, strikethrough, code content isolation).
    - Code fences with language sanitization and mid-stream unclosed handling.
    - Security allowlist enforcement (blocking `javascript:`, `data:`, `vbscript:` schemes; escaping `<script>`, `<img>`, `<iframe>`).
    - Multi-kind event routing (`X.applyEvents`) verifying Dispatched prompt, User prompt, Agent message, and Thinking details fold.
    - Prompt clamp/expand interactions.
    - Streaming batch contract verifying exactly 1 `.msg-body` write per flush.
- **`tests/test_dashboard_page.py`**:
  - Python-side contract tests assert:
    - `test_markdown_rendering_all_four_kinds`
    - `test_markdown_safety_contract`
    - `test_markdown_typography_covers_all_kinds`

---

## 3. Verification Log

| # | Command | Scope | Result | Details |
|---|---|---|---|---|
| 1 | `node tests/dashboard_status_behavior.js` | VM behavioral suite | **PASS** | Exit code 0; all assertions passed (~830 tests including 42 Markdown tests). |
| 2 | `uv run pytest tests/test_dashboard_page.py` | Full page integration | **PASS** | Exit code 0; 76 passed in 20.46s. |
| 3 | `node scripts/bench_dashboard_replay.js` | JSDOM replay bench | **PASS** | Exit code 0; `msgBodySets: 1`, `railOk: true`, `jsError: false`. |
| 4 | Adversarial Security Suite (TEMP probe) | 40+ XSS / DOM injections | **PASS** | 0 script/iframe elements injected; 0 inline event handlers; 0 unsafe link schemes. |
| 5 | ReDoS & Pathological Stress (TEMP probe) | 50k tokens / deep nesting | **PASS** | Completed in 1.85s; linear scaling; no call-stack exhaustion. |
| 6 | Incremental Streaming Prefix Probe | 331 character prefixes | **PASS** | 0 uncaught exceptions or parse faults mid-stream. |

---

## 4. Final Acceptance Verdict

All four timeline text kinds (Dispatched message, User message, Agent, and Thinking) now render rich Markdown through a single, secure, escape-first renderer. All six adversarial invariants hold.

ACCEPT
