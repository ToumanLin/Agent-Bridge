from __future__ import annotations

import contextlib
import importlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO


class FileLockTimeout(TimeoutError):
    pass


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    fcntl = importlib.import_module("fcntl")

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    fcntl = importlib.import_module("fcntl")

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: Path, *, timeout_sec: float = 5.0) -> Iterator[None]:
    """Hold an advisory lock that is shared by processes using this path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_sec
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise FileLockTimeout(f"timed out waiting for lock {path}")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle)

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
    """Read JSON without treating an unreadable or invalid file as an empty one.

    A missing file is ``default``. Invalid JSON, or a denial that outlives the
    retry budget, raises: a writer merging into a shared file must never mistake
    "locked" or "corrupt" for "empty" — that would drop sibling rows.
    """
    delay = REPLACE_RETRY_DELAY_SEC
    for attempt in range(REPLACE_RETRY_ATTEMPTS):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        except PermissionError:
            # The same transient Windows lock class as _replace_with_retry: a
            # concurrent atomic replace can deny the open for a moment.
            if attempt + 1 >= REPLACE_RETRY_ATTEMPTS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, REPLACE_RETRY_MAX_DELAY_SEC)
            continue
        return json.loads(text)
    return default


def read_json(path: Path, default: Any) -> Any:
    """Lenient read: anything unreadable or invalid, including a lock that
    outlasts the retry budget, reads as ``default``."""
    try:
        return read_json_strict(path, default)
    except (OSError, ValueError):
        return default
