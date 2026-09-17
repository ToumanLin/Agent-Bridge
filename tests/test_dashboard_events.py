"""Characterization tests for the dashboard_events extraction.

The transcript-normalization and live-usage domain lives in
``agent_bridge.share.dashboard_events`` while ``agent_bridge.share.dashboard``
re-exports it. These tests pin the two contracts that decoupling could
silently break: the live-tail cache object is shared between modules, and
rebinding ``dashboard.TRANSCRIPT_DIR`` redirects every read — the wrappers
must pass the current value rather than a copy taken at import time.
"""

import json
import subprocess
import sys
from pathlib import Path

from agent_bridge.share import dashboard, dashboard_events


def _usage_rec(ts, total):
    return {
        "type": "usage",
        "ts": ts,
        "data": {"consumed": {"scope": "run", "total": total}},
    }


def test_live_tail_cache_is_shared():
    """``dashboard._LIVE_TAIL`` is the same dict the tail reader mutates —
    tests clear and inspect it through the dashboard module."""
    assert dashboard._LIVE_TAIL is dashboard_events._LIVE_TAIL
    assert dashboard._LIVE_LOCK is dashboard_events._LIVE_LOCK
    assert dashboard.LIVE_SEED_BYTES == dashboard_events.LIVE_SEED_BYTES
    assert dashboard.normalize_event is dashboard_events.normalize_event
    assert dashboard._usage_consumed_last is dashboard_events._usage_consumed_last


def test_rebound_transcript_dir_directs_reads(tmp_path, monkeypatch):
    """Rebinding ``dashboard.TRANSCRIPT_DIR`` must redirect both event reads
    and the live-usage scan to the new directory."""
    transcripts = tmp_path / "bridge" / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "sess_1.jsonl").write_text(
        json.dumps({"type": "message_chunk", "ts": "t", "data": {"text": "hi"}}) + "\n",
        encoding="utf-8",
    )
    (transcripts / "sess_live.jsonl").write_text(
        json.dumps(_usage_rec("2026-09-12T10:00:20Z", 7)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "TRANSCRIPT_DIR", transcripts)
    dashboard._LIVE_TAIL.clear()

    events, offset = dashboard.read_events("sess_1", 0)
    assert events == [{"t": "msg", "ts": "t", "text": "hi"}]
    assert offset > 0

    live = dashboard.live_usage_map(
        [
            {
                "task_id": "task_1",
                "session_id": "sess_live",
                "status": "running",
                "started_at": "2026-09-12T10:00:00Z",
            }
        ]
    )
    assert live["sess_live"]["consumed"]["total"] == 7
    assert dashboard._LIVE_TAIL["sess_live"]["offset"] > 0


def test_dashboard_py_runs_as_a_script():
    """``python share/dashboard.py`` runs with no package context; the events
    module must resolve as a sibling on sys.path and argparse must work."""
    script = Path(dashboard.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--port" in result.stdout and "--dir" in result.stdout
