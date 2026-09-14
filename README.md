<p align="center">
  <img src="logo.svg" alt="Agent Bridge" width="420">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-green.svg" alt="Python 3.11+"></a>
</p>

<p align="center">
  <a href="#english">English</a> · <a href="#中文">中文</a>
</p>

## English

Agent Bridge is a connector for local coding agents. A coordinator — Codex, Cursor, Kimi Code, ZCode, Grok Build, Claude Code, or Devin — directs Antigravity CLI, Grok Build, Kimi Code, DeepSeek Harness, OpenCode, Claude Code, Codex CLI, and Devin CLI. The same product can be a coordinator and a worker; those are different processes. More agents will follow.

```text
User → Coordinator (Codex / Cursor / Kimi Code / ZCode / Grok Build / Claude Code / Devin)
     → Agent Bridge (MCP) → Antigravity CLI
                          → Grok Build
                          → Kimi Code
                          → DeepSeek Harness
                          → OpenCode
                          → Claude Code
                          → Codex CLI
                          → Devin CLI
```

It does not drive GUIs. The user talks only to the coordinator.

### Install

Need [uv](https://docs.astral.sh/uv/). Then:

```powershell
uv tool install git+https://github.com/FeiZhuLulu/Agent-Bridge.git
```

### Connect Codex

```powershell
codex mcp add agent_bridge -- %USERPROFILE%\.local\bin\agent-bridge.exe
```

Restart Codex. The coordinator skill is written the first time the server starts. More hosts and proxy notes: [SETUP.md](SETUP.md).

### Connect Cursor

`%USERPROFILE%\.cursor\mcp.json` (all projects) or `<repo>\.cursor\mcp.json`:

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "C:/Users/YOU/.local/bin/agent-bridge.exe"
    }
  }
}
```

Devin (Desktop or CLI) reads this same `~/.cursor/mcp.json`, so it needs no entry of its own.

### Connect Kimi Code

`%USERPROFILE%\.kimi-code\mcp.json` (all projects) or `<repo>\.kimi-code\mcp.json`:

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "C:/Users/YOU/.local/bin/agent-bridge.exe",
      "toolTimeoutMs": 600000
    }
  }
}
```

### Connect ZCode, Grok Build, or Claude Code

Resolve the real executable first (`Get-Command agent-bridge | Select-Object -ExpandProperty Source`). ZCode UI paste JSON, ZCode native `config.json`, Grok `config.toml`, and Claude Code `.mcp.json` / `~/.claude.json` are different shapes — do not mix them. Copy-paste blocks: [SETUP.md](SETUP.md).

### Connect another agent

A plain ACP CLI needs no code. Add a block to `~/.agent-bridge/agents.toml`:

```toml
[agents.mycustom]
protocol = "acp"
command = ["mycustom-cli", "acp"]
revivable = true
```

Full process, including agents that need adapter changes: [skills/add-worker/SKILL.md](skills/add-worker/SKILL.md).

### Update

Close coordinators that are holding Bridge, then `agent-bridge upgrade`, then restart them.

### Tools

| Tool | Role |
| --- | --- |
| `list_agents` | Probe workers, report remaining quota, proxy/env + coordinator policy |
| `set_preferences` | Persist coordinator mode / routing preferences |
| `dispatch_task` | Start or resume a turn in the project `cwd` |
| `wait_task` | Block up to `timeout_sec` (default 180); a timeout ends the call, never the worker |
| `check_task` | Non-blocking status |
| `get_result` | Complete final result in pages + changed files |
| `get_transcript` | Paged session log |
| `cancel_task` | Cancel the in-flight turn |
| `pause_task` | End the turn gracefully; keep it resumable |
| `resume_task` | Continue a paused/cancelled/failed task on the same session |
| `list_sessions` | Known sessions |
| `list_tasks` | This instance's tasks plus sibling-owned ones (`remote` / `owner` metadata) |
| `end_session` | Shut down a worker process |

`get_result` returns up to 60,000 characters per call. Continue with
`next_cursor` while `has_more` is true. Detailed work events remain available
through `get_transcript` and `~/.agent-bridge/transcripts/`; sparse task
lifecycle and error summaries use the rotating files in
`~/.agent-bridge/logs/`. Transcript writes are buffered and reads do not flush
them to disk. Buffered events reach disk after 64 KB or 30 s, or as soon as a
turn ends; a normal stop flushes everything, so only a crash or a hard kill can
lose that last window. A turn whose worker stays silent past `stall_timeout_sec`
(default 1800 s, per worker) ends `failed` / `stalled`; `check_task` shows
`silent_for_sec`.

A pending `wait_task` never polls the model — local waits are event-driven
inside Bridge — but each returned timeout is a model-visible turn. For a long
task the token-free pattern is dispatch-and-later-check: `dispatch_task`, end
the turn, then `check_task` / `get_result` on a later one. Bridge cannot wake a
dormant coordinator at completion; that takes a host scheduler or a later user
turn. A single ~90-minute wait (`timeout_sec=5400`) works only when the host
MCP tool timeout exceeds it with margin and the call executes directly — Codex
Code Mode turns it into repeated model-visible yields. Details:
[SETUP.md](SETUP.md#waiting-and-timeouts).

Tasks keep their owning Bridge instance. After a coordinator restart, a task a
live sibling still owns shows `remote: true` in `list_tasks` / `check_task` /
`wait_task` / `get_result` — the new instance can poll it and read its result,
but never runs or cancels it; `owner_lost: true` marks one whose owner died
mid-run. `pause_task` ends an in-flight turn gracefully — the row lands
terminal (`cancelled` / `stop_reason: "paused"`) with its partial result,
transcript, and session intact — and `resume_task` continues it as a new,
linked task on the same conversation. A dead owner's `resumable` row can be
resumed too: the next instance adopts the session and reaps its orphaned
worker before respawning, so two executors never drive one conversation. With
opt-in `[server] shutdown_policy = "linger"`, an orderly host shutdown (stdin
EOF) lets in-flight turns finish first, bounded by `linger_max_sec` (default
86400 s); linger cannot defeat a host force-kill of the Bridge process tree —
park the turn with `pause_task` first, then `resume_task` after the restart.

### Remaining quota

Each `list_agents` row carries `quota`: `status` ok / exhausted / unknown, the
CLI's rolling windows with `remaining_percent` and `resets_at`, and an optional
`balance` when supported. Codex (app-server) and Kimi Code (`kimi login`)
are read out of the box; Grok Build and Claude
Code sit behind `[quota] experimental = true`; everything else answers
`unknown` with the reason. DSH balance lookup is not supported because its
active provider and account can vary. Bridge reports, it does not route — the coordinator
weighs it against your instructions. Lookups are bounded by `[quota]
timeout_sec` and cached up to `cache_sec` or the earliest window reset. Custom API/auth endpoints return `unknown` before credentials or cached quota are used; `agent-bridge --quota` prints a fresh
reading. Details: [SETUP.md](SETUP.md#remaining-quota-in-list_agents).

Claude's overall status covers its shared limits. Model-specific weekly limits
remain in `windows`; check the requested model's window before dispatch, even
when the overall status is `ok`.

### Coordinator mode

Three levels. Default `auto`. First connect does not ask, and does not write `mode` into your overlay.

- `manual` — only when you explicitly ask; Bridge rejects the rest
- `auto` — the coordinator decides
- `eager` — prefer dispatching multi-step work; the coordinator still accepts

Change it in chat, or set `[coordinator] mode` in `~/.agent-bridge/agents.toml`. Per-host: `AGENT_BRIDGE_MODE` in that host's MCP `env`. Lasting routing ("research goes to antigravity") is saved the same way. Details: [SETUP.md](SETUP.md).

### Orchestration rulebook

[ORCHESTRATION.md](ORCHESTRATION.md) is the coordinator rulebook: when to dispatch, to whom, how to verify. The skill and MCP handshake instructions are projections of it. First start writes the skill automatically. Chinese: [ORCHESTRATION.zh-CN.md](ORCHESTRATION.zh-CN.md).

### Tests

```powershell
uv run pytest
```

`tests/` is pytest only. Live coordinator drills use a local `lab/` folder from `scripts/setup_lab.py` — that directory is not in git.

## 中文

Agent Bridge 是一个联通各个本地 Agent 的连接器。由协调者——Codex、Cursor、Kimi Code、ZCode、Grok Build、Claude Code 或 Devin——指挥 Antigravity CLI、Grok Build、Kimi Code、DeepSeek Harness、OpenCode、Claude Code、Codex CLI、Devin CLI 进行工作。同一个产品可以同时是协调者和 Worker，但那是不同进程。后续将推出更多 Agent 支持。

```text
用户 → 协调者（Codex / Cursor / Kimi Code / ZCode / Grok Build / Claude Code / Devin）
     → Agent Bridge (MCP) → Antigravity CLI
                          → Grok Build
                          → Kimi Code
                          → DeepSeek Harness
                          → OpenCode
                          → Claude Code
                          → Codex CLI
                          → Devin CLI
```

它不操作图形界面。用户只和协调者对话。

### 安装

先装 [uv](https://docs.astral.sh/uv/)，然后：

```powershell
uv tool install git+https://github.com/FeiZhuLulu/Agent-Bridge.git
```

### 接到 Codex

```powershell
codex mcp add agent_bridge -- %USERPROFILE%\.local\bin\agent-bridge.exe
```

重启 Codex。协调者 skill 会在服务器第一次启动、以及升级后内容变化时自动写入；平时重启不会覆盖你的本地改动。其它宿主和代理见 [SETUP.md](SETUP.md)。

### 接到 Cursor

`%USERPROFILE%\.cursor\mcp.json`（所有项目）或 `<仓库>\.cursor\mcp.json`：

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "C:/Users/YOU/.local/bin/agent-bridge.exe"
    }
  }
}
```

Devin（Desktop 或 CLI）会直接读这份 `~/.cursor/mcp.json`，不需要单独配置。

### 接到 Kimi Code

`%USERPROFILE%\.kimi-code\mcp.json`（所有项目）或 `<仓库>\.kimi-code\mcp.json`：

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "C:/Users/YOU/.local/bin/agent-bridge.exe",
      "toolTimeoutMs": 600000
    }
  }
}
```

### 接到 ZCode、Grok Build 或 Claude Code

先解析真实可执行文件（`Get-Command agent-bridge | Select-Object -ExpandProperty Source`）。ZCode 弹窗 JSON、ZCode 原生 `config.json`、Grok 的 `config.toml`、Claude Code 的 `.mcp.json` / `~/.claude.json` 是不同结构，不能混用。完整示例见 [SETUP.md](SETUP.md)。

### 接入其它 Agent

普通 ACP CLI 不用改代码，在 `~/.agent-bridge/agents.toml` 加一段：

```toml
[agents.mycustom]
protocol = "acp"
command = ["mycustom-cli", "acp"]
revivable = true
```

完整流程（含需要改适配器的情况）见 [skills/add-worker/SKILL.md](skills/add-worker/SKILL.md)。

### 更新

先关掉正连着 Bridge 的协调者，执行 `agent-bridge upgrade`，再重启。

### 工具

| 工具 | 作用 |
| --- | --- |
| `list_agents` | 探测 worker，报告剩余额度、代理 / 环境 + 协调者策略 |
| `set_preferences` | 持久化协调者模式 / 路由偏好 |
| `dispatch_task` | 在项目 `cwd` 里开始或续上一次回合 |
| `wait_task` | 最多等待 `timeout_sec`（默认 180）；超时只结束本次调用，不会杀 Worker |
| `check_task` | 非阻塞状态查询 |
| `get_result` | 分页读取完整结果 + 改过的文件 |
| `get_transcript` | 分页会话日志 |
| `cancel_task` | 取消进行中的回合 |
| `pause_task` | 优雅结束当前回合，保留可续性 |
| `resume_task` | 在同一会话上继续已暂停/取消/失败的任务 |
| `list_sessions` | 已知会话 |
| `list_tasks` | 本实例任务 + 兄弟实例持有的任务（`remote` / `owner` 元数据） |
| `end_session` | 关掉 worker 进程 |

`get_result` 每次最多返回 60,000 个字符；`has_more` 为 true 时，用
`next_cursor` 继续读取。详细工作事件仍可通过 `get_transcript` 和
`~/.agent-bridge/transcripts/` 查询；任务生命周期及错误摘要写入
`~/.agent-bridge/logs/` 下的轮转日志。转录采用缓冲批量写入，读取不会触发刷盘。
缓冲事件在累计 64 KB、间隔 30 秒或一轮结束时落盘；正常停止会全部刷出，只有崩溃或被强杀才可能丢掉最后这一窗口。
Worker 静默超过 `stall_timeout_sec`（默认 1800 秒，可按 Worker 设置）的一轮会以
`failed` / `stalled` 结束；`check_task` 会给出 `silent_for_sec`。

挂起的 `wait_task` 不会轮询模型——本地等待在 Bridge 内部由事件驱动——但每次
超时返回都是一个模型可见的回合。长任务的零轮询模式是「派发后稍后再查」：
`dispatch_task` 之后结束本轮，稍后 `check_task` / `get_result`。Bridge 无法
在任务完成时唤醒休眠的协调者——得靠宿主定时机制或用户的下一轮。单次约
90 分钟的等待（`timeout_sec=5400`）只在宿主 MCP 工具超时高于它并留余量、
且该调用直接执行时成立——Codex Code Mode 会把它切成一串模型可见的 yield。
细节见 [SETUP.md](SETUP.md#waiting-and-timeouts)。

任务始终归属创建它的 Bridge 实例。协调者重启后，仍由其他存活实例持有的任务在
`list_tasks` / `check_task` / `wait_task` / `get_result` 中显示 `remote: true`
——新实例可以继续跟踪并读取其结果，但不会运行或取消它；`owner_lost: true`
表示持有方已中途退出。`pause_task` 会优雅结束在途回合——任务行落为终态
（`cancelled` / `stop_reason: "paused"`），部分结果、转录和会话都保留；
`resume_task` 再以同一会话上的新任务继续。持有方已死的 `resumable` 任务
也能续：下一个实例会先收养其会话、清掉孤儿 worker 再重新拉起，同一会话不会
出现两个执行体。开启 `[server] shutdown_policy = "linger"` 后，正常关闭
宿主（stdin EOF）会先等在途回合跑完，上限为 `linger_max_sec`（默认 86400 秒）；
linger 无法对抗宿主强杀整个 Bridge 进程树——要跨重启保活，先 `pause_task`，
重启后再 `resume_task`。

### 剩余额度

`list_agents` 每一行都带 `quota`：`status` 为 ok / exhausted / unknown，各个滚动窗口的
`remaining_percent` 和 `resets_at`（重置时间），支持时还可提供 `balance`。
Codex（app-server）、Kimi Code（`kimi login`）
开箱即读；Grok Build 和 Claude Code 需要 `[quota] experimental = true`；其余 Worker
返回 `unknown` 并附原因。DSH 的供应商和账户可以变化，暂不支持余额查询。Bridge 只报告、不路由——协调者结合你的 instructions 自己权衡。
自定义 API／认证端点不支持额度查询，直接返回 `unknown`，不会读取官方凭据或旧额度缓存。
每次查询受 `[quota] timeout_sec` 限制，缓存最迟在 `cache_sec` 秒或最近窗口重置时失效；`agent-bridge --quota`
可以打印一份不走缓存的读数。细节见 [SETUP.md](SETUP.md#remaining-quota-in-list_agents)。

Claude 的整体状态只汇总通用额度；模型专属周额度保留在 `windows` 中。
即使整体为 `ok`，派发前仍需检查指定模型的窗口。

### 协调者档位

三档。默认 `auto`。第一次接入不会询问，也不会把 `mode` 写进 overlay。

- `manual` — 只在你明确要求时才派；其它派发会被 Bridge 拒绝
- `auto` — 协调者自己判断
- `eager` — 多步工作优先派出去；验收仍是协调者

对话里改，或在 `~/.agent-bridge/agents.toml` 写 `[coordinator] mode`。单个宿主不同档：该宿主 MCP 的 `env` 里设 `AGENT_BRIDGE_MODE`。长久路由（「调研都给 antigravity」）同样在对话里说一次即可。细节见 [SETUP.md](SETUP.md)。

### 协调规则书

[ORCHESTRATION.md](ORCHESTRATION.md) 是给协调者的规则书：什么时候派、派给谁、怎么验收。skill 和 MCP 握手 instructions 是它的投影。第一次启动会自动写入 skill。中文译本：[ORCHESTRATION.zh-CN.md](ORCHESTRATION.zh-CN.md)。

### 测试

```powershell
uv run pytest
```

`tests/` 只跑 pytest。真实协调者联调用本机 `lab/`（`scripts/setup_lab.py` 生成），这个目录不进 git。

## License

[MIT](LICENSE) © FeiZhuLulu
