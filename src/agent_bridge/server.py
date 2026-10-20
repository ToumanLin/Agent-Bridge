from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version as package_version
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations

from agent_bridge import public
from agent_bridge.dashboard import maybe_open_dashboard
from agent_bridge.logging_setup import setup_logging
from agent_bridge.models import DEFAULT_WAIT_SEC
from agent_bridge.paths import ensure_home
from agent_bridge.registry import RESULT_PAGE_MAX_CHARS, Registry

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    open_world_hint=False,
    idempotent_hint=True,
)


@asynccontextmanager
async def lifespan(_server: MCPServer[Registry]) -> AsyncIterator[Registry]:
    home = ensure_home()
    setup_logging(home)
    registry = Registry.create(home)
    await registry.start()
    try:
        yield registry
    finally:
        await registry.stop()


# Injected into the coordinator's context at the MCP handshake. Together with
# the tool descriptions it is the whole contract: no skill file is installed.
INSTRUCTIONS = (
    "Agent Bridge runs local worker CLIs (Grok, Kimi Code, Antigravity, OpenCode, "
    "Claude Code, Codex CLI, Devin CLI, DeepSeek Harness) as your subagents. "
    "Reach workers only through these tools, never by running their CLIs. "
    "Call list_agents first; if dispatch_enabled is false you are inside a "
    "worker: do not dispatch, cancel, or set preferences. dispatch_task.cwd is "
    "this project's absolute path. A running wait_task answer is not a failure; "
    "wait again. Verify a worker's work with your own git diff and tests. "
    "Tasks with owner sibling belong to another Bridge: only wait on them."
)

# How much result text a finished wait_task / cancel_task carries inline, so
# most turns need no get_result call.
FIRST_PAGE_CHARS = 20_000

# Rows list_tasks returns by default; Registry keeps up to 200 finished tasks
# plus every live sibling's, and a coordinator wants the recent few.
LIST_TASKS_LIMIT = 20

mcp = MCPServer[Registry](
    "agent-bridge",
    version=package_version("agent-bridge"),
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)


def _registry(ctx: Context) -> Registry:
    lifespan_ctx = ctx.request_context.lifespan_context
    if isinstance(lifespan_ctx, Registry):
        lifespan_ctx.touch_activity()
        maybe_open_dashboard(lifespan_ctx.home, lifespan_ctx.config.dashboard)
        return lifespan_ctx
    raise RuntimeError("Agent Bridge registry is not available")


def _failure(exc: Exception) -> RuntimeError:
    """A plain message for the MCP error result (a KeyError would add quotes)."""
    text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
    return RuntimeError(str(text) or type(exc).__name__)


def _with_first_page(registry: Registry, view: dict[str, Any]) -> dict[str, Any]:
    if view.get("status") in public.RUNNING:
        return view
    page = registry.get_result(view["task_id"], cursor=0, max_chars=FIRST_PAGE_CHARS)
    return public.with_result(view, page)


@mcp.tool(annotations=READ_ONLY)
async def list_agents(ctx: Context) -> dict[str, Any]:
    """Workers with availability, remaining quota (status ok/exhausted/unknown; unknown means unreadable, not empty) and fixed per-worker notes, plus the user's routing mode/instructions and warnings. Call first."""
    try:
        registry = _registry(ctx)
        return public.agents_view(
            await registry.list_agents(),
            registry.env_status(),
            registry.coordinator_status(),
        )
    except Exception as exc:
        raise _failure(exc) from exc


@mcp.tool()
async def set_preferences(
    ctx: Context,
    mode: str | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Persist a lasting user preference. mode: manual (dispatch only on request) / auto / eager. instructions replaces the stored routing text: merge with list_agents.instructions first. Not for one-off wishes."""
    try:
        result = _registry(ctx).set_preferences(mode=mode, instructions=instructions)
        coordinator = result.get("coordinator") or {}
        # The instructions text is what the caller just sent; only the mode is news.
        return {"mode": coordinator.get("mode")}
    except Exception as exc:
        raise _failure(exc) from exc


@mcp.tool()
async def dispatch_task(
    ctx: Context,
    message: str | None = None,
    agent: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    continue_task: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    title: str | None = None,
    user_requested: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Start a worker turn; returns at once. New: agent, cwd, message. Follow-up: add session_id. Resume an unfinished (cancelled/failed) task: continue_task=task_id, message optional. user_requested: user explicitly asked (manual mode). request_id: UUID for retry dedup."""
    try:
        registry = _registry(ctx)
        if continue_task:
            extra = [
                name
                for name, value in (
                    ("agent", agent),
                    ("cwd", cwd),
                    ("session_id", session_id),
                    ("model", model),
                    ("effort", effort),
                )
                if value
            ]
            if extra:
                raise ValueError(f"continue_task takes only message and request_id, not {', '.join(extra)}")
            result = await registry.resume_task(continue_task, message=message, request_id=request_id)
        else:
            if not agent or not cwd or not (message or "").strip():
                raise ValueError("agent, cwd and message are required unless continue_task is given")
            result = await registry.dispatch_task(
                agent=agent,
                message=message,
                cwd=cwd,
                session_id=session_id,
                model=model,
                effort=effort,
                title=title,
                user_requested=user_requested,
                request_id=request_id,
            )
        warnings = registry.quota_warnings(str(result.get("agent") or agent or ""), result.get("model") or model)
        return public.dispatch_view(result, warnings)
    except Exception as exc:
        raise _failure(exc) from exc


@mcp.tool(annotations=READ_ONLY)
async def wait_task(ctx: Context, task_id: str, timeout_sec: float = DEFAULT_WAIT_SEC) -> dict[str, Any]:
    """Wait up to timeout_sec (0 = just check) for a task. Running: progress only; call again. Finished: status, files_changed, usage and the first page of result; next_cursor means get_result has more. Keep timeout_sec under the host's tool timeout."""
    try:
        registry = _registry(ctx)
        if timeout_sec <= 0:
            snapshot = registry.check_task(task_id)
        else:
            snapshot = await registry.wait_task(task_id, timeout_sec=timeout_sec)
        return _with_first_page(registry, public.task_view(snapshot))
    except Exception as exc:
        raise _failure(exc) from exc


@mcp.tool(annotations=READ_ONLY)
async def get_result(
    ctx: Context,
    task_id: str,
    cursor: int = 0,
    max_chars: int = RESULT_PAGE_MAX_CHARS,
) -> dict[str, Any]:
    """A page of a finished task's result text from cursor (max_chars up to 60000). Continue with next_cursor until it is absent."""
    try:
        return public.result_view(_registry(ctx).get_result(task_id, cursor=cursor, max_chars=max_chars))
    except Exception as exc:
        raise _failure(exc) from exc


@mcp.tool()
async def cancel_task(ctx: Context, task_id: str) -> dict[str, Any]:
    """End a running task gracefully: partial result and conversation are kept, and dispatch_task(continue_task=task_id) can resume it. A finished task is returned unchanged."""
    try:
        registry = _registry(ctx)
        return _with_first_page(registry, public.task_view(await registry.stop_task(task_id)))
    except Exception as exc:
        raise _failure(exc) from exc


@mcp.tool(annotations=READ_ONLY)
async def list_tasks(
    ctx: Context,
    active_only: bool = False,
    cwd: str | None = None,
    limit: int = LIST_TASKS_LIMIT,
) -> dict[str, Any]:
    """Known tasks, newest first, to find task_ids again after a restart. active_only keeps queued/running ones; cwd keeps tasks run there; limit caps rows (0 = all), tasks_more counts the rest."""
    try:
        registry = _registry(ctx)
        return public.tasks_view(
            registry.list_tasks(active_only=active_only),
            registry.session_titles(),
            limit=limit,
            cwd=cwd,
        )
    except Exception as exc:
        raise _failure(exc) from exc


def main() -> None:
    from agent_bridge.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
