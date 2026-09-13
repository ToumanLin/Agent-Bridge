"""Minimal ACP echo agent used for offline adapter tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from acp import run_agent, update_agent_message_text
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    Usage,
    UsageUpdate,
)

_COG = "cognition.ai/"


def _usage_updates(turn: int) -> list[UsageUpdate]:
    """Devin-shaped UsageUpdate stream, emitted when ECHO_USAGE is set.

    Root stream cumulative input + per-step output, a subagent stream keyed by
    subagent_context.runId, and one exact paired re-emission. Counters are
    conversation-cumulative: later turns continue from where the last ended.
    """
    if not os.environ.get("ECHO_USAGE"):
        return []

    def upd(used: int, meta: dict[str, Any]) -> UsageUpdate:
        return UsageUpdate(
            used=used, size=100, sessionUpdate="usage_update", field_meta=meta
        )

    sub = {_COG + "subagent_context": {"runId": "sub-1", "parentAgentId": "root"}}
    if turn == 0:
        return [
            upd(5, {_COG + "inputTokens": 10, _COG + "outputTokens": 4}),
            upd(8, {_COG + "inputTokens": 20, **sub}),
            upd(9, {_COG + "inputTokens": 25, _COG + "outputTokens": 9}),
            upd(9, {_COG + "inputTokens": 25, _COG + "outputTokens": 9}),
            upd(12, {_COG + "inputTokens": 35, _COG + "outputTokens": 3, **sub}),
        ]
    return [
        upd(13, {_COG + "inputTokens": 30, _COG + "outputTokens": 13}),
        upd(14, {_COG + "inputTokens": 40, _COG + "outputTokens": 6, **sub}),
    ]


class EchoAgent:
    def __init__(self) -> None:
        self._conn: Any = None
        self._session_id = "echo-session"
        self._turns: dict[str, int] = {}

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                prompt_capabilities=PromptCapabilities(image=False, audio=False, embedded_context=False),
                load_session=True,
            ),
            agent_info=Implementation(name="echo", version="0.0.1"),
        )

    async def new_session(self, cwd: str, mcp_servers=None, **kwargs: Any) -> NewSessionResponse:
        return NewSessionResponse(session_id=self._session_id)

    async def load_session(self, cwd: str, session_id: str, mcp_servers=None, **kwargs: Any) -> None:
        # ECHO_FAIL_LOAD simulates a worker that cannot reload a persisted
        # native session: the bridge must fall through to session/new and
        # treat the turn as a fresh conversation.
        if os.environ.get("ECHO_FAIL_LOAD"):
            raise RuntimeError("echo load_session failed")
        self._session_id = session_id
        return None

    def _turn(self, session_id: str) -> int:
        """This prompt's turn index.

        ECHO_USAGE_STATE points at a JSON file carrying the counter across a
        worker respawn — like a provider whose UsageUpdate counters are
        conversation-cumulative no matter which process answers.
        """
        path = os.environ.get("ECHO_USAGE_STATE")
        if path:
            try:
                return int(json.loads(Path(path).read_text(encoding="utf-8")).get("turn", 0))
            except (OSError, ValueError, TypeError):
                pass
        return self._turns.get(session_id, 0)

    def _save_turn(self, session_id: str, turn: int) -> None:
        self._turns[session_id] = turn + 1
        path = os.environ.get("ECHO_USAGE_STATE")
        if path:
            with contextlib.suppress(OSError):
                Path(path).write_text(json.dumps({"turn": turn + 1}), encoding="utf-8")

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        text = ""
        for block in prompt:
            piece = getattr(block, "text", None)
            if isinstance(piece, str):
                text += piece
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                text += block["text"]
        if self._conn is not None:
            try:
                await self._conn.session_update(
                    session_id=session_id,
                    update=update_agent_message_text(f"echo:{text}"),
                )
            except TypeError:
                await self._conn.session_update(
                    session_id,
                    update_agent_message_text(f"echo:{text}"),
                )
            turn = self._turn(session_id)
            self._save_turn(session_id, turn)
            for update in _usage_updates(turn):
                try:
                    await self._conn.session_update(session_id=session_id, update=update)
                except TypeError:
                    await self._conn.session_update(session_id, update)
        # ECHO_FAIL_PROMPT fails the turn after the usage updates landed —
        # the bridge must keep the partial run_usage on the failed task.
        if os.environ.get("ECHO_FAIL_PROMPT"):
            raise RuntimeError("echo prompt exploded")
        return PromptResponse(
            stop_reason="end_turn",
            usage=Usage(total_tokens=3, input_tokens=1, output_tokens=2),
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


async def _main() -> None:
    await run_agent(EchoAgent())


if __name__ == "__main__":
    asyncio.run(_main())
