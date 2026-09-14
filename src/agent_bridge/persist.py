from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

# Windows transiently denies the destination rename while an antivirus scan,
# an indexer, or a concurrent reader (dashboard, sibling Bridge) holds the
# target open — PermissionError WinError 5/32 on os.replace. A short bounded
# retry absorbs those; a persistent denial still propagates unchanged.
REPLACE_RETRY_ATTEMPTS = 6
REPLACE_RETRY_DELAY_SEC = 0.05
REPLACE_RETRY_MAX_DELAY_SEC = 0.4


def _replace_with_retry(tmp: Path, path: Path) -> None:
    delay = REPLACE_RETRY_DELAY_SEC
    for attempt in range(REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 >= REPLACE_RETRY_ATTEMPTS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, REPLACE_RETRY_MAX_DELAY_SEC)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        _replace_with_retry(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json_strict(path: Path, default: Any) -> Any:
    """read_json that surfaces a persistent PermissionError instead of
    collapsing to ``default``. Writers merging into a shared file must not
    turn "temporarily locked" into "empty" — that would drop sibling rows."""
    if not path.is_file():
        return default
    delay = REPLACE_RETRY_DELAY_SEC
    for attempt in range(REPLACE_RETRY_ATTEMPTS):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            # The same transient Windows lock class as _replace_with_retry: a
            # concurrent atomic replace can deny the open for a moment.
            if attempt + 1 >= REPLACE_RETRY_ATTEMPTS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, REPLACE_RETRY_MAX_DELAY_SEC)
        except (OSError, json.JSONDecodeError):
            return default
    return default


def read_json(path: Path, default: Any) -> Any:
    try:
        return read_json_strict(path, default)
    except PermissionError:
        # Read-side callers keep the lenient contract: a file that stays
        # locked past the retry budget reads as absent, never as corrupt.
        return default
