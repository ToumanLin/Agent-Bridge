from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

CONVERSATION_ID = "conv-fake-agy"


def _brain_write(obj) -> None:
    """Append one record to the fake conversation's brain transcript.

    FAKE_AGY_BRAIN points at the directory that stands in for
    ~/.gemini/antigravity-cli/brain in tests.
    """
    root = os.environ.get("FAKE_AGY_BRAIN")
    if not root:
        return
    path = (
        Path(root)
        / CONVERSATION_ID
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(obj if isinstance(obj, str) else json.dumps(obj))
        fh.write("\n")


def _fail(reason: str) -> int:
    print(reason, file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    args = argv[1:]
    if os.environ.get("FAKE_AGY_MODE") == "early_exit":
        print("fake agy refused to start", file=sys.stderr, flush=True)
        return 3

    if "--input-format" not in args or args[args.index("--input-format") + 1] != "stream-json":
        return _fail("missing --input-format stream-json")
    if "--output-format" not in args or args[args.index("--output-format") + 1] != "stream-json":
        return _fail("missing --output-format stream-json")
    if "-p" not in args:
        return _fail("missing -p")
    if args[args.index("-p") + 1] != "":
        return _fail("-p must be followed by an empty string")

    raw = sys.stdin.readline()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(f"stdin is not JSON: {exc}")
    if not isinstance(obj, dict):
        return _fail("stdin JSON must be an object")
    if obj.get("event") != "user":
        return _fail('event must be "user"')
    message = obj.get("message")
    if not isinstance(message, dict):
        return _fail("message must be an object")
    if message.get("role") != "user":
        return _fail('message.role must be "user"')
    content = message.get("content")
    if not isinstance(content, str):
        return _fail("message.content must be a string")

    report = os.environ.get("FAKE_AGY_REPORT")
    if report:
        with open(report, "w", encoding="utf-8") as handle:
            handle.write(str(len(content)))

    echo = "echo:" + content[:16]
    if os.environ.get("FAKE_AGY_MODE") == "thinking":
        # Real agy appends thinking to the brain log as it reasons; a record
        # can land before the stdout init that reveals the conversation id.
        _brain_write({"type": "PLANNER_RESPONSE", "thinking": "first thought"})
    print(json.dumps({"event": "init", "conversation_id": CONVERSATION_ID}), flush=True)
    if os.environ.get("FAKE_AGY_MODE") == "hang":
        # Holds the turn open so tests can exercise the cancellation path.
        while True:
            time.sleep(60)
    if os.environ.get("FAKE_AGY_MODE") == "thinking":
        delay = float(os.environ.get("FAKE_AGY_BRAIN_DELAY", "0"))
        if delay:
            time.sleep(delay)
        # Forward-compatible stdout thought shape; the adapter must neither
        # emit it twice (the brain log already carried the same text) nor let
        # its text_delta leak into the assistant answer.
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 1,
                        "state": "DONE",
                        "step_type": "thought",
                        "text_delta": "first thought",
                    },
                }
            ),
            flush=True,
        )
        _brain_write({"type": "PLANNER_RESPONSE", "thinking": "second thought"})
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 2,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": echo,
                    },
                }
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": CONVERSATION_ID,
                        "status": "SUCCESS",
                        "response": echo,
                        "usage": {"total_tokens": 5},
                    },
                }
            ),
            flush=True,
        )
        # Written after the last stdout record; only the post-exit drain is
        # guaranteed to observe it.
        _brain_write({"type": "PLANNER_RESPONSE", "thinking": "late thought"})
        return 0
    if os.environ.get("FAKE_AGY_MODE") == "rich":
        # The observed agy stream shape: a tool step pair (ACTIVE then DONE
        # with tool_info.output), a usage-only agent_response DONE carrying no
        # text_delta, then the text and the final result.
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 1,
                        "state": "ACTIVE",
                        "step_type": "tool",
                        "tool_name": "find_by_name",
                        "tool_info": {
                            "name": "find_by_name",
                            "parameters": {"Pattern": "*.py", "SearchDirectory": "C:/repo"},
                        },
                    },
                }
            )
        )
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 1,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "find_by_name",
                        "tool_info": {"name": "find_by_name", "output": "a.py\nb.py"},
                    },
                }
            )
        )
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 2,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "usage": {"total_tokens": 40},
                    },
                }
            )
        )
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 3,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": echo,
                    },
                }
            )
        )
        print(
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": CONVERSATION_ID,
                        "status": "SUCCESS",
                        "response": echo,
                        "usage": {"total_tokens": 42},
                    },
                }
            )
        )
        return 0
    if os.environ.get("FAKE_AGY_MODE") == "capacity_503":
        # The observed provider-capacity failure: streamed text, an opaque
        # mid-turn error_message step agy absorbed, more text, then a fatal
        # ERROR result carrying the verbatim 503 body — process exit code 0.
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 1,
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": echo,
                    },
                }
            )
        )
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 2,
                        "state": "DONE",
                        "step_type": "error_message",
                    },
                }
            )
        )
        print(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONVERSATION_ID,
                        "step_index": 3,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "...partial report",
                    },
                }
            )
        )
        print(
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": CONVERSATION_ID,
                        "status": "ERROR",
                        "response": echo + "...partial report",
                        "error": (
                            "API error (attempt 1): UNAVAILABLE (code 503): "
                            "No capacity available for model gemini-3.8-flash-medium "
                            "on the server"
                        ),
                        "code": 0,
                    },
                }
            )
        )
        return 0
    print(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": CONVERSATION_ID,
                    "step_type": "agent_response",
                    "text_delta": echo,
                },
            }
        )
    )
    print(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": CONVERSATION_ID,
                    "status": "SUCCESS",
                    "response": echo,
                    "usage": {"total_tokens": 1},
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
