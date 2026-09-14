import os
from pathlib import Path

import pytest

from agent_bridge.persist import (
    REPLACE_RETRY_ATTEMPTS,
    atomic_write_text,
    read_json,
    read_json_strict,
)


def test_atomic_write_text_replaces_and_leaves_no_tmp(tmp_path: Path):
    target = tmp_path / "note.txt"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert list(tmp_path.glob("*.tmp*")) == []


def test_atomic_write_retries_transient_permission_error(tmp_path: Path, monkeypatch):
    """Windows AV/indexer/reader locks deny os.replace briefly — the write
    retries and lands once the destination frees up."""
    target = tmp_path / "note.txt"
    real_replace = os.replace
    attempts = 0

    def flaky(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(13, "Access is denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert attempts == 3
    assert list(tmp_path.glob("*.tmp*")) == []


def test_atomic_write_propagates_persistent_permission_error(tmp_path: Path, monkeypatch):
    """A denial that outlives the bounded backoff still raises — nothing is
    masked, and the temp file is cleaned up."""
    attempts = 0

    def denied(src, dst):
        nonlocal attempts
        attempts += 1
        raise PermissionError(13, "Access is denied", str(dst))

    monkeypatch.setattr(os, "replace", denied)
    target = tmp_path / "note.txt"
    with pytest.raises(PermissionError):
        atomic_write_text(target, "hello")
    assert attempts == REPLACE_RETRY_ATTEMPTS
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp*")) == []


def test_atomic_write_does_not_retry_non_permission_errors(tmp_path: Path, monkeypatch):
    """Only PermissionError is retried; other OSErrors propagate at once."""
    attempts = 0

    def boom(src, dst):
        nonlocal attempts
        attempts += 1
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(tmp_path / "note.txt", "hello")
    assert attempts == 1


def test_read_json_retries_transient_permission_error(tmp_path: Path, monkeypatch):
    """A concurrent atomic replace can deny the open for a moment; the read
    retries instead of reporting a phantom empty state."""
    target = tmp_path / "state.json"
    target.write_text('{"tasks": [{"task_id": "t1"}]}', encoding="utf-8")
    real_read_text = Path.read_text
    attempts = 0

    def flaky(self, *args, **kwargs):
        nonlocal attempts
        if self == target and attempts < 2:
            attempts += 1
            raise PermissionError(13, "Access is denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert read_json(target, {}) == {"tasks": [{"task_id": "t1"}]}
    assert attempts == 2


def test_read_json_strict_raises_persistent_permission_error(tmp_path: Path, monkeypatch):
    """The strict variant surfaces a denial that outlives the budget — a
    writer merging into the file must never mistake locked for empty. The
    lenient variant still falls back to its default."""
    target = tmp_path / "state.json"
    target.write_text('{"tasks": []}', encoding="utf-8")

    def denied(self, *args, **kwargs):
        raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        read_json_strict(target, {})
    assert read_json(target, {"fallback": True}) == {"fallback": True}
