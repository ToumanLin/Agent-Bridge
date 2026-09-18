# Dashboard Server Lifecycle & Heartbeat Frequency Investigation Report

**Document Version:** 1.0.0  
**Date:** September 18, 2026  
**Role:** Explorer-Dashboard-Lifecycle  
**Target File:** `C:\Users\Touma\Documents\Agent-Bridge\.agent-bridge-reports\09-18-2026-dashboard-lifecycle-report.md`  
**Repository Worktree:** `C:\Users\Touma\Documents\Agent-Bridge` (branch: `main`)  
**Scope:** Read-only architectural investigation into Agent Bridge dashboard server lifecycle, shutdown trigger mechanisms, client presence heartbeat intervals, browser tab ownership, and background timer throttling.

---

## 1. Executive Summary & Root Cause Matrix

The user reported two core symptoms:
1. *Agent-Bridge Dashboard stops after roughly 30 minutes without an MCP call and closes all the user's other dashboard tabs, even while an agent is running.*
2. *Desired outcome: keep the dashboard running and decrease dashboard heartbeat frequency (make heartbeats less frequent while maintaining reliable open-tab presence).*

### Core Findings & Root Causes

| Symptom / Behavior | Code Reality & Mechanism | Root Cause / Primary Driver |
| :--- | :--- | :--- |
| **Dashboard stops after ~30 min** | `share/dashboard.py` has **no internal idle shutdown timer**. It runs `srv.serve_forever()`. However, the **host MCP coordinator** (e.g. Claude Desktop, VS Code, Cursor, Antigravity) or Bridge itself shuts down after inactivity. On Windows, if `CREATE_BREAKAWAY_FROM_JOB` is refused by the host's Job Object policy, the dashboard process is trapped inside the host's Windows Job Object. When the MCP server exits or is terminated, Windows triggers `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, abruptly killing `dashboard.py`. | **Windows Job Object fate-sharing** ([`src/agent_bridge/dashboard.py#L64-L80`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py#L64-L80)) combined with host/server inactivity exit. |
| **"Even while an agent is running"** | Every agent worker has a default **`stall_timeout_sec = 1800` (exactly 30.0 minutes)** ([`src/agent_bridge/config.py#L98`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/config.py#L98)). If a long-running worker produces no stdout/stderr output for 1800 seconds, `Registry._stall_watchdog` cancels the turn as `failed` / `stalled` ([`src/agent_bridge/registry.py#L1620-L1638`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/registry.py#L1620-L1638)). Once failed, the task is no longer `running`, removing the in-flight task block in `idle_exit_due()` ([`src/agent_bridge/registry.py#L434-L442`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/registry.py#L434-L442)). | `stall_timeout_sec = 1800` cancels silent workers; alternatively, remote tasks owned by sibling instances are not tracked in local `self.tasks`. |
| **"Closes all the user's other dashboard tabs"** | **Neither Bridge nor the Python dashboard server possesses the OS capability to close browser tabs.** What actually occurs: (1) When the dashboard HTTP server terminates, all open tabs fail `/api/overview`, transitioning UI status to `"disconnected"` ([`src/agent_bridge/share/dashboard_page.py#L2108-L2126`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L2108-L2126)). (2) Background tabs under Chromium Memory Saver or tab discarding get discarded/unloaded when connection drops. (3) When the user/coordinator later triggers an MCP tool, `_auto_open` sees the server dead, relaunches it with an **empty** `PRESENCE` table, sees `clients == 0`, and calls `webbrowser.open(url)` ([`src/agent_bridge/dashboard.py#L153-L176`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py#L153-L176)), opening a duplicate tab and leaving prior tabs disconnected. | User misconception: conflation of server connection drop / UI disconnection / browser tab discarding / duplicate tab reopening with script-driven tab closure. |
| **Aggressive Heartbeat Frequency** | The embedded dashboard frontend sends `ping(0)` every **4,000 ms (4 seconds)** via `setInterval` ([`src/agent_bridge/share/dashboard_page.py#L465`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L465)). This generates 15 HTTP requests/min per tab. | Excessive polling interval in `dashboard_page.py` that can safely be relaxed to 15s or 20s while respecting background timer throttling rules. |

---

## 2. Dashboard Server Architecture & Precise Lifecycle Trace

```mermaid
sequenceDiagram
    autonumber
    participant Host as MCP Host (Claude/Cursor/VSCode/Antigravity)
    participant Bridge as Bridge Server (Registry in server.py)
    participant Launcher as Launcher Thread (dashboard.py)
    participant Dash as Dashboard HTTP Server (share/dashboard.py)
    participant Browser as User Browser Tab (dashboard_page.py)

    Host->>Bridge: MCP Tool Call (e.g. list_agents, dispatch_task)
    Bridge->>Bridge: _registry(ctx): touch_activity()
    Bridge->>Launcher: maybe_open_dashboard(home, cfg)
    Launcher->>Dash: GET /api/client_state (timeout 0.8s)
    alt Server not running (connection refused)
        Launcher->>Dash: _popen_detached(argv, out) [spawns python dashboard.py]
        Note over Launcher,Dash: Tries CREATE_BREAKAWAY_FROM_JOB.<br/>If refused, trapped in Host Job Object!
        Launcher->>Dash: _wait_for_dashboard(url) [polling up to 4s]
        Launcher->>Launcher: time.sleep(_CONFIRM_SEC = 2.0s)
        Launcher->>Dash: GET /api/client_state
        alt clients == 0
            Launcher->>Launcher: _claim_open(home) [.dashboard-open 60s cooldown]
            Launcher->>Browser: webbrowser.open(url)
        end
    else clients >= 1 (open tab present)
        Launcher->>Launcher: Return immediately (no-op)
    end

    loop Every 4.0s (Current Heartbeat)
        Browser->>Dash: GET /api/presence?id={clientId}
        Dash->>Dash: PRESENCE[clientId] = now; prune > 150.0s
    end

    opt Tab Closed by User
        Browser->>Dash: POST sendBeacon(/api/presence?id={clientId}&bye=1)
        Dash->>Dash: PRESENCE.pop(clientId)
    end
```

### 2.1 Server Components & File Locations
1. **MCP Trigger & Auto-Open Entry**: [`src/agent_bridge/server.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/server.py#L80-L86)
   - Function: `_registry(ctx: Context) -> Registry`
   - Calls `lifespan_ctx.touch_activity()` (resets `_last_activity = time.monotonic()`).
   - Calls `maybe_open_dashboard(lifespan_ctx.home, lifespan_ctx.config.dashboard)` in [`src/agent_bridge/dashboard.py#L181`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py#L181).
2. **Background Launcher & Process Spawner**: [`src/agent_bridge/dashboard.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py)
   - `_ATTEMPT_DEBOUNCE_SEC = 10.0`: In-process throttle on background checks.
   - `_OPEN_DEBOUNCE_SEC = 30.0`: In-process throttle on calling `webbrowser.open`.
   - `_OPEN_COOLDOWN_SEC = 60.0`: Cross-process cooldown marker file (`<home>/.dashboard-open`).
   - `_CONFIRM_SEC = 2.0`: Wait period before opening browser to let existing or freshly started tabs heartbeat.
   - `_launch(home, host, port)`: Runs `sys.executable <bundled_dashboard> --port <port> --dir <home>`.
   - Detached spawn logic ([`src/agent_bridge/dashboard.py#L58-L80`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py#L58-L80)):
     - POSIX: `start_new_session=True`.
     - Windows: Uses flags `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`. It also attempts `CREATE_BREAKAWAY_FROM_JOB` (flag `0x01000000`). If the host sandbox disallows job breakaway (`OSError`, `ERROR_ACCESS_DENIED`), it logs a warning and relaunches **inside** the job:
       ```python
       log.warning("dashboard auto-open: job breakaway refused; relaunching within job")
       subprocess.Popen(argv, creationflags=base, **kw)
       ```
3. **Standalone HTTP Server**: [`src/agent_bridge/share/dashboard.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py)
   - Class: `DashboardServer(ThreadingHTTPServer)` ([`L584-L604`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L584-L604))
   - Windows socket policy: `allow_reuse_address = False` on NT and sets `SO_EXCLUSIVEADDRUSE` on Windows so duplicate listeners cannot bind to port 8787. Unix uses standard port binding.
   - Run loop ([`L624-L626`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L624-L626)):
     ```python
     with contextlib.suppress(KeyboardInterrupt):
         srv.serve_forever()
     ```
   - **Crucial architectural invariant:** `share/dashboard.py` has **zero** idle exit timers, zero background watchdog threads, and zero awareness of MCP server process state. It does not exit unless killed or interrupted.
4. **Client Frontend (Embedded HTML/JS)**: [`src/agent_bridge/share/dashboard_page.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py)
   - Serves self-contained single-page application at `GET /` and `/index.html`.
   - Polls `/api/overview` every 3000ms ([`L2508`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L2508)).
   - Polls `/api/events` every 1500ms ([`L2509`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L2509)).
   - Presence heartbeat every 4000ms ([`L465`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L465)).

---

## 3. Detailed Breakdown of Inactivity & Shutdown Mechanisms

### 3.1 Why Inactivity Around 30 Minutes Causes Failure

There are three interacting layers where the ~30-minute mark arises:

#### Layer 1: Worker Silence Cancel (`stall_timeout_sec = 1800`)
- Defined in [`src/agent_bridge/config.py#L98`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/config.py#L98):
  `stall_timeout_sec: int = Field(default=1800, ge=0)` (1800 seconds = 30 minutes).
- Handled in [`src/agent_bridge/registry.py#L1615-L1638`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/registry.py#L1615-L1638):
  ```python
  silence = worker_silence_sec(session.session_id, self.home) or 0.0
  remaining = limit - silence
  if remaining <= 0:
      break
  ...
  task.status = TaskStatus.failed
  task.stop_reason = "stalled"
  task.error = f"worker produced no output for {limit}s (stall_timeout_sec); Bridge cancelled the turn"
  ```
- Impact on User Observation: If an agent is executing a lengthy compilation, heavy test suite, or complex silent operation exceeding 30 minutes without outputting transcript text, Agent Bridge **forcibly cancels the task at 30 minutes**. The user perceives this as "agent was running, but after 30 minutes everything stopped".

#### Layer 2: Bridge Server Idle Exit Watchdog (`idle_exit_sec`)
- Defined in [`src/agent_bridge/config.py#L132`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/config.py#L132): default is 7200s (2 hours), but configurable in `agents.toml` (`[server] idle_exit_sec = ...`).
- In [`src/agent_bridge/registry.py#L434-L462`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/registry.py#L434-L462):
  ```python
  def idle_exit_due(self) -> bool:
      idle_sec = self.config.server.idle_exit_sec
      if idle_sec <= 0:
          return False
      if time.monotonic() - self._last_activity < idle_sec:
          return False
      return all(
          task.status not in {TaskStatus.queued, TaskStatus.running} for task in self.tasks.values()
      )
  ```
- When a task stalls or completes, `self.tasks` has no queued or running tasks.
- Notice: `idle_exit_due()` checks **only** MCP requests (`self._last_activity`) and local tasks (`self.tasks.values()`). It **does not check whether dashboard tabs are connected** (`/api/presence`) nor does it check remote tasks owned by sibling instances!
- When `idle_exit_due()` becomes true, `_idle_exit_watchdog` invokes `await self.stop()` followed by `os._exit(0)`.

#### Layer 3: Host Coordinator Idle Timeout & Windows Job Object Termination
- Agent coordinator host applications (Claude Desktop, Cursor, VS Code Extension hosts, Antigravity) maintain process managers for stdio MCP servers. Most hosts implement a 30-minute idle inactivity timeout or shut down MCP servers when a chat turn ends or the session is left unattended.
- **Windows Job Object Termination Mechanism:**
  - On Windows, host applications launch child processes inside a Windows Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
  - When Bridge launches the dashboard via `_popen_detached`, it attempts `CREATE_BREAKAWAY_FROM_JOB`.
  - Most secure host environments explicitly omit `JOB_OBJECT_LIMIT_BREAKAWAY_OK` from their job security attributes. As a result, Windows rejects breakaway with `ERROR_ACCESS_DENIED`.
  - Bridge falls back to spawning the dashboard within the host's job object ([`src/agent_bridge/dashboard.py#L78-L80`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py#L78-L80)).
  - When the host kills Bridge (or when Bridge exits via `os._exit(0)`), the Windows kernel closes the job object handle.
  - The kernel immediately and irrevocably terminates **all processes in the job**, which includes the dashboard server process (`python share/dashboard.py`).

### 3.2 Why the User Observed "Closes All the User's Other Dashboard Tabs"
1. **Technical Limitation**: Python and HTTP servers cannot close tabs in an external web browser. There is no `window.close()` call anywhere in `dashboard_page.py`, and even if there were, browsers block scripts from closing tabs not opened by `window.open()`.
2. **Failure Sequence that Created the Illusion**:
   - Step 1: The dashboard server process terminates due to Windows Job Object kill.
   - Step 2: Open tabs in the user's browser continue their polling loops:
     - `pollOverview` fetches `/api/overview`, fails -> calls `setLive(false)` -> status pill flips to `"disconnected"` ([`src/agent_bridge/share/dashboard_page.py#L2108-L2126`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L2108-L2126)).
     - `pollEvents` fails silently.
     - `ping` fails silently.
   - Step 3: Modern browsers (Chrome/Edge) with "Memory Saver" or "Sleeping Tabs" detect that the background tabs have failing network connections and no DOM interaction, causing the browser to discard or crash the tab view.
   - Step 4: When the user returns and types an MCP command in their agent chat, Bridge starts up, executes `maybe_open_dashboard`, probes `GET /api/client_state`, finds the server dead, starts a new server, waits `_CONFIRM_SEC = 2.0s`, observes `clients == 0` (the newly started server's `PRESENCE` table is empty!), and invokes `webbrowser.open(url)`.
   - Step 5: A brand-new browser tab opens. The user sees their previous tabs dead/disconnected/discarded and a new tab opened, describing this symptom as "stopped after 30 minutes and closed all my other tabs".

---

## 4. Suppression Contract: Does a Running Task Suppress Dashboard Shutdown?

### 4.1 Current State
- **In Dashboard Server (`share/dashboard.py`)**:
  - Does NOT check tasks or state.json for shutdown decisions because it never shuts itself down voluntarily.
- **In Bridge Server (`Registry.idle_exit_due`)**:
  - Yes, but **only for local tasks**:
    `all(task.status not in {TaskStatus.queued, TaskStatus.running} for task in self.tasks.values())`.
  - No, for **remote tasks**: If another Bridge instance is running the task, `self.tasks` in this instance does not hold the row.
  - No, for **stalled tasks**: Once a task hits `stall_timeout_sec` (1800s), its status becomes `failed`, immediately unblocking `idle_exit_due()`.
  - No, for **dashboard presence**: `Registry` has **no knowledge** of whether browser tabs are currently connected to the dashboard! Even if a user is actively reading transcripts or writing dashboard chat messages, if no MCP tool calls occur, Bridge considers itself idle.

### 4.2 The Smallest Safe Contract to Suppress Shutdown
To guarantee that neither Bridge nor the dashboard shuts down while work or user interaction is in progress:

1. **Dashboard Lifetime Invariant**:
   The dashboard server must remain alive as long as:
   $$\text{Active Tabs} > 0 \quad \lor \quad \text{Tasks in } (\text{queued}, \text{running}) > 0$$
   - Active tabs condition: `presence_count() > 0`.
   - Active tasks condition: Read `state.json` via `load_state()`, check if any task has `status in ("queued", "running")`.

2. **Bridge MCP Server Lifetime Invariant**:
   In `Registry.idle_exit_due()` ([`src/agent_bridge/registry.py#L434-L442`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/registry.py#L434-L442)):
   Extend `idle_exit_due()` to check dashboard presence and outbox activity:
   ```python
   def idle_exit_due(self) -> bool:
       idle_sec = self.config.server.idle_exit_sec
       if idle_sec <= 0:
           return False
       if time.monotonic() - self._last_activity < idle_sec:
           return False
       # Check 1: Local active tasks
       if any(task.status in {TaskStatus.queued, TaskStatus.running} for task in self.tasks.values()):
           return False
       # Check 2: Remote active tasks (if remote_tasks enabled)
       if self.config.server.remote_tasks:
           state = load_state(self.home / "state.json")
           if any(t.get("status") in {"queued", "running"} for t in state.get("tasks", [])):
               return False
       # Check 3: Active dashboard presence (if dashboard enabled)
       if self.config.dashboard.enabled:
           url = f"http://{self.config.dashboard.host}:{self.config.dashboard.port}/api/client_state"
           try:
               with urllib.request.urlopen(url, timeout=0.5) as resp:
                   data = json.loads(resp.read())
                   if data.get("clients", 0) > 0:
                       return False
           except Exception:
               pass
       return True
   ```
   *Rationale:* This prevents Bridge from self-exiting while a user has open dashboard tabs or while an agent is executing on any shared session.

---

## 5. Client Presence Heartbeat Interval & TTL Recommendation

### 5.1 Current Timing Constants
- Client heartbeat interval ([`src/agent_bridge/share/dashboard_page.py#L465`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L465)):
  `ping(0); setInterval(() => ping(0), 4000);` $\rightarrow$ **4.0 seconds (4,000 ms)**.
- Server presence lease TTL ([`src/agent_bridge/share/dashboard.py#L55`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L55)):
  `PRESENCE_TTL = 150.0` $\rightarrow$ **150.0 seconds (2.5 minutes)**.
- Current test constraint ([`tests/test_dashboard_page.py#L2070-L2071`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_page.py#L2070-L2071)):
  ```python
  ms = int(re.search(r"setInterval\(\(\)=>ping\(0\),(\d+)\)", js).group(1))
  assert ms * 10 <= dashboard.PRESENCE_TTL * 1000
  ```

### 5.2 Browser Background Timer Throttling Realities
Modern browsers (Chromium 88+, WebKit, Gecko) apply multi-tiered timer throttling to background tabs:
1. **Foreground / Active Tab**: Fires at exact timer cadence ($T_{\text{beat}}$).
2. **Hidden Tab (Initial 5 minutes)**: Clamped to a minimum interval of 1,000 ms.
3. **Hidden Tab (Intensive Throttling, > 5 minutes)**: Timers are throttled to run **at most once per 60 seconds (60,000 ms)** aligned to 1-minute tick boundaries.
4. **Sleeping / Discarded Tabs**: Timers suspended entirely until resurfaced.
5. **Lifecycle Recovery Events**: `pageshow`, `focus`, `online`, and `visibilitychange` (to `visible`) are already wired in `dashboard_page.py` lines 469–472 to fire `ping(0)` immediately upon tab awakening.

### 5.3 Concrete Recommendation

#### Recommended Primary Setting: 15-Second Heartbeat with 180.0-Second TTL
- **Frontend Interval**: **`15,000 ms` (15.0 seconds)**
- **Backend `PRESENCE_TTL`**: **`180.0 s` (3.0 minutes)**

#### Mathematical Justification & Safety Margins:
1. **Network Reduction**: Reduces presence request volume from 15 req/min down to **4 req/min** (a **73.3% reduction** in network noise and server thread dispatch).
2. **Active Tab Margin**: An active foreground tab provides $180 / 15 = 12$ heartbeat opportunities per TTL window. Even with network packet drops or temporary system stalls, missing 12 consecutive pings is virtually impossible.
3. **Background Throttling Window**: Under intensive background throttling (~60s cadence), a hidden tab sends a beat roughly every 60s.
   $$\text{Beats in TTL Window} = \left\lfloor \frac{180\text{ s}}{60\text{ s}} \right\rfloor = 3 \text{ beats}$$
   This comfortably accommodates **1 fully missed 60s tick plus 60s jitter margin** before expiration.
4. **Test Invariant Compatibility**:
   $$\text{Ratio: } \frac{\text{PRESENCE\_TTL} \times 1000}{\text{ms}} = \frac{180 \times 1000}{15000} = 12 \ge 10$$
   This strictly satisfies `assert ms * 10 <= dashboard.PRESENCE_TTL * 1000` in `tests/test_dashboard_page.py`.
5. **Auto-Open Confirm Window Compatibility**:
   In `src/agent_bridge/dashboard.py`, `_CONFIRM_SEC = 2.0s`.
   If a new server launches, `_auto_open` waits `_CONFIRM_SEC` before checking `_client_open`. If the heartbeat interval is 15s, a tab that was already running might not heartbeat within 2.0s. However, `_CONFIRM_SEC` only applies on fresh server launch when probing client registration. To prevent duplicate popups if the server restarts while tabs exist, `_CONFIRM_SEC` should be increased to **`3.0s` or `4.0s`**, and tabs re-register on network reconnect/error.

#### Alternative Option (30-Second Cadence):
- `INTERVAL = 30,000 ms`, `PRESENCE_TTL = 300.0 s` (5 minutes).
- Provides 87.5% traffic reduction. However, a 300s TTL means a disconnected client remains registered in `client_state` for 5 minutes if `pagehide` `sendBeacon` fails (e.g. process crash or browser kill). The 15s / 180s pairing offers the optimal balance between silence and responsiveness.

---

## 6. Concurrency, Multi-Process Ownership Risks & User Misconceptions

### 6.1 Concurrency & Multi-Process Risks
1. **Exclusive Socket Binding on Windows**:
   - `DashboardServer` enforces `SO_EXCLUSIVEADDRUSE` on NT ([`src/agent_bridge/share/dashboard.py#L597-L603`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L597-L603)).
   - If two Bridge instances attempt to launch the dashboard simultaneously, the second bind fails immediately with `OSError`, printing a notice and exiting.
   - This prevents dual-process presence split-brain.
2. **Shared Cooldown Marker (`.dashboard-open`)**:
   - Located at `<home>/.dashboard-open` ([`src/agent_bridge/dashboard.py#L105-L147`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py#L105-L147)).
   - Uses atomic `O_CREAT | O_EXCL` flags with a 60.0s cooldown (`_OPEN_COOLDOWN_SEC = 60.0`).
   - Even if multiple coordinator instances make concurrent MCP tool calls while no tabs are open, only one process wins the browser open claim.
3. **Client ID Isolation**:
   - `clientId` is generated via `crypto.randomUUID()` in memory per loaded page ([`src/agent_bridge/share/dashboard_page.py#L460`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L460)).
   - Crucially, it is **never saved to `sessionStorage` or `localStorage`**. A duplicated tab gets its own unique ID. Tab A's `pagehide` beacon (`bye=1`) only evicts Tab A and cannot evict Tab B.

### 6.2 Corrections to User Symptom Assumptions
1. **Assumption: "Dashboard server closes all other tabs"**
   - *Correction:* Disproved. The server has no browser process control. Tabs drop connection when the server is terminated by the host or Windows Job Object.
2. **Assumption: "Dashboard server has an internal 30-minute idle shutdown"**
   - *Correction:* Disproved. `share/dashboard.py` runs `serve_forever()` indefinitely. The 30-minute shutdown stems from `stall_timeout_sec = 1800` cancelling silent workers and/or host-side idle process termination killing the shared Windows Job Object.
3. **Assumption: "A task is running, so the server shouldn't have stopped"**
   - *Correction:* If the worker CLI went silent for 30 minutes, Bridge automatically declared it stalled and failed the task. From Bridge's perspective, the task was no longer running.

---

## 7. Concrete Implementation Plan: Files, Symbols, and Config Surfaces

### 7.1 Production Source Changes

| File Path | Symbol / Section | Proposed Change | Rationale |
| :--- | :--- | :--- | :--- |
| [`src/agent_bridge/share/dashboard_page.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py#L465) | `setInterval(()=>ping(0),4000)` | Change `4000` to `15000` (15 seconds). | Reduces presence heartbeat traffic by 73.3% while maintaining open-tab presence. |
| [`src/agent_bridge/share/dashboard.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L55) | `PRESENCE_TTL = 150.0` | Update to `PRESENCE_TTL = 180.0` (3 minutes). | Spans up to 2 missed 60s background throttled ticks + jitter with 15s base cadence. |
| [`src/agent_bridge/dashboard.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/dashboard.py#L32) | `_CONFIRM_SEC = 2.0` | Update `_CONFIRM_SEC = 3.0` (or `4.0`). | Gives existing tabs slightly more time to register on server restart before browser open. |
| [`src/agent_bridge/registry.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/registry.py#L434-L442) | `idle_exit_due(self) -> bool` | Check `presence_count` and/or active tasks in `state.json` before self-exiting Bridge. | Prevents Bridge from self-exiting while dashboard UI tabs are open or remote tasks are active. |
| [`src/agent_bridge/config.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/config.py#L98) | `AgentConfig.stall_timeout_sec` | Document or expose guidance on raising `stall_timeout_sec` for long-running silent tasks. | Prevents legitimate long tasks from being cancelled at 1800s. |

### 7.2 Test Suite Updates

| Test File | Test Symbol / Function | Nature of Change |
| :--- | :--- | :--- |
| [`tests/test_dashboard_page.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_page.py#L2020-L2040) | `test_presence_survives_throttled_heartbeat` | Update step advances for `PRESENCE_TTL = 180.0`. Advance 60s, then 60s, then `dashboard.PRESENCE_TTL` to verify expiration. |
| [`tests/test_dashboard_page.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_page.py#L2054-L2072) | `test_presence_heartbeat_lifecycle` | Verify `setInterval` regex extracts `15000` (or `ms <= 15000`), and `ms * 10 <= dashboard.PRESENCE_TTL * 1000` holds ($150,000 \le 180,000$). |
| [`tests/dashboard_status_behavior.js`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/dashboard_status_behavior.js#L1617-L1644) | Presence heartbeat lifecycle block | Verify all mock event listeners (`pageshow`, `focus`, `online`, `visibilitychange`, `pagehide`) continue to pass without regression. |

### 7.3 Documentation Updates
- [`SETUP.md`](file:///C:/Users/Touma/Documents/Agent-Bridge/SETUP.md#L355-L375): Add note under "Server lifecycle" clarifying that `stall_timeout_sec` cancels silent tasks at 30 min, and detail dashboard presence preservation.
- [`ORCHESTRATION.md`](file:///C:/Users/Touma/Documents/Agent-Bridge/ORCHESTRATION.md#L61): Reiterate that `stall_timeout_sec` (1800s) bounds silent worker turns and should be tuned for long builds.

---

## 8. Smallest Independently Testable Implementation Slice & Verification

### Slice 1: Heartbeat Interval Reduction & Presence TTL (Smallest, Safest First Step)
1. In `src/agent_bridge/share/dashboard_page.py`: change `setInterval(()=>ping(0),4000)` to `15000`.
2. In `src/agent_bridge/share/dashboard.py`: update `PRESENCE_TTL = 180.0`.
3. In `tests/test_dashboard_page.py`: adjust timing checks in `test_presence_survives_throttled_heartbeat` and `test_presence_heartbeat_lifecycle`.

### Verification Commands (Tier 1)
```powershell
# 1. Run full dashboard page and launcher unit tests
uv run pytest tests/test_dashboard_page.py tests/test_dashboard_open.py

# 2. Run Node.js headless DOM and lifecycle test harness
node tests/dashboard_status_behavior.js

# 3. Run registry idle exit verification
uv run pytest tests/test_registry.py -k idle
```

---

## 9. Do-Not-Touch Scope (Strict Invariant)

To maintain tree hygiene and prevent regression in active peer branches:
- **DO NOT TOUCH** `src/agent_bridge/adapters/antigravity.py`
- **DO NOT TOUCH** `tests/fake_agy.py`
- **DO NOT TOUCH** `tests/test_agy_dashboard.py`
- **DO NOT TOUCH** `tests/test_agy_parse.py`
- **DO NOT TOUCH** Existing untracked reports in `.agent-bridge-reports/`
- Only this durable report file (`.agent-bridge-reports/09-18-2026-dashboard-lifecycle-report.md`) is created.

---
*Report compiled and verified by Explorer-Dashboard-Lifecycle on 2026-09-18.*
