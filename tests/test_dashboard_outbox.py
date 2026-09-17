"""Characterization tests for the dashboard_outbox extraction.

The outbox/state/process-ownership domain lives in
``agent_bridge.share.dashboard_outbox`` while ``agent_bridge.share.dashboard``
re-exports it. These tests pin the contracts the decoupling could silently
break: shared object identity between the modules, dynamic rebinding of
``dashboard.STATE_FILE`` / ``dashboard.OUTBOX_DIR``, ``dashboard.open`` and
``dashboard.psutil`` injection, and the single create-time cache staying
readable through the dashboard module without a second copy.
"""

import json
import os
import time

import pytest

from agent_bridge.share import dashboard, dashboard_events, dashboard_outbox


def test_outbox_symbols_are_shared():
    """The re-exports are the same objects, not copies — and there is still
    exactly one SAFE_ID regex serving the whole package."""
    assert dashboard.MSG_NAME_RE is dashboard_outbox.MSG_NAME_RE
    assert dashboard.REQ_NAME_RE is dashboard_outbox.REQ_NAME_RE
    assert dashboard.OUTBOX_MSG_MAX_AGE_SEC == dashboard_outbox.OUTBOX_MSG_MAX_AGE_SEC
    assert dashboard.TASK_ACTION_EXPIRE_SEC == dashboard_outbox.TASK_ACTION_EXPIRE_SEC
    assert dashboard.SAFE_ID is dashboard_events.SAFE_ID
    assert dashboard_outbox.SAFE_ID is dashboard_events.SAFE_ID


def _write_msg(outbox, name, **fields):
    rec = {"session_id": "sess_1", "message": "hi", "ts": time.time()}
    rec.update(fields)
    (outbox / name).write_text(json.dumps(rec), encoding="utf-8")


def test_outbox_queue_reads_rebound_dir(tmp_path, monkeypatch):
    """Rebinding ``dashboard.OUTBOX_DIR`` must redirect the queue scan — the
    wrapper passes the current value, not a copy taken at import time."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    monkeypatch.setattr(dashboard, "OUTBOX_DIR", outbox)

    assert dashboard.outbox_queue() == {}
    _write_msg(outbox, "msg_2_abcd1235.json", session_id="sess_2")
    _write_msg(outbox, "msg_1_abcd1234.json", ts=5.0, state="waiting_busy", attempts=2)
    # Non-queue names never surface: done records, claims, stray files, and
    # records whose session_id fails SAFE_ID are all filtered out.
    _write_msg(outbox, "msg_3_abcd1236.json", session_id="../evil")
    (outbox / "msg_4_abcd1237.json.99.claim").write_text("{}", encoding="utf-8")
    (outbox / "random.json").write_text("{}", encoding="utf-8")
    (outbox / "msg_5_abcd1238.json").write_text("not json", encoding="utf-8")

    queue = dashboard.outbox_queue()
    assert set(queue) == {"sess_1", "sess_2"}
    item = queue["sess_1"][0]
    assert item["name"] == "msg_1_abcd1234.json"
    assert item["state"] == "waiting_busy"
    assert item["attempts"] == 2

    other = tmp_path / "other_outbox"
    other.mkdir()
    _write_msg(other, "msg_6_abcd1239.json", session_id="sess_9")
    monkeypatch.setattr(dashboard, "OUTBOX_DIR", other)
    assert set(dashboard.outbox_queue()) == {"sess_9"}


def test_outbox_queue_sorts_by_ts_then_name(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    _write_msg(outbox, "msg_2_00000002.json", ts=20.0)
    _write_msg(outbox, "msg_3_00000003.json", ts=10.0)
    _write_msg(outbox, "msg_1_00000001.json", ts=10.0)
    queue = dashboard_outbox.outbox_queue(outbox)
    names = [i["name"] for i in queue["sess_1"]]
    # ts groups first; the same ts falls back to the filename.
    assert names == [
        "msg_1_00000001.json",
        "msg_3_00000003.json",
        "msg_2_00000002.json",
    ]


def test_outbox_queue_missing_dir_is_empty(tmp_path):
    assert dashboard_outbox.outbox_queue(tmp_path / "nope") == {}


def test_load_state_reads_rebound_file(tmp_path, monkeypatch):
    """Rebinding ``dashboard.STATE_FILE`` redirects the read; unreadable or
    missing files keep the original empty-shape fallback."""
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"sessions": [{"session_id": "s"}], "tasks": [{"task_id": "t"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "STATE_FILE", state_file)
    assert dashboard.load_state() == {
        "sessions": [{"session_id": "s"}],
        "tasks": [{"task_id": "t"}],
    }

    monkeypatch.setattr(dashboard, "STATE_FILE", tmp_path / "missing.json")
    assert dashboard.load_state() == {"sessions": [], "tasks": []}

    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(dashboard, "STATE_FILE", bad)
    assert dashboard.load_state() == {"sessions": [], "tasks": []}


def test_load_state_uses_dashboard_open(tmp_path, monkeypatch):
    """``dashboard.open`` is injected as the open callable so monkeypatching
    it still intercepts the state-file read."""
    state_file = tmp_path / "state.json"
    state_file.write_text('{"sessions": [], "tasks": []}', encoding="utf-8")
    monkeypatch.setattr(dashboard, "STATE_FILE", state_file)
    opens = []
    real_open = open

    def spy(file, *args, **kwargs):
        opens.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(dashboard, "open", spy, raising=False)
    assert dashboard.load_state() == {"sessions": [], "tasks": []}
    assert opens == [str(state_file)]


def test_create_time_cache_is_single_and_live(monkeypatch):
    """``dashboard._MY_CREATE_TIME*`` are live views of the outbox module's
    one cache — no diverging copy exists on the dashboard module."""
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME", 12.5)
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME_SET", True)
    assert dashboard._MY_CREATE_TIME == 12.5
    assert dashboard._MY_CREATE_TIME_SET is True
    assert "_MY_CREATE_TIME" not in dashboard.__dict__


def test_my_create_time_caches_and_uses_dashboard_psutil(monkeypatch):
    """``dashboard.psutil`` is passed at call time: a fake psutil drives the
    stamp, the result caches, and failures degrade to the pid-only shape."""
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME", None)
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME_SET", False)
    calls = []

    class _FakeProc:
        def create_time(self):
            return 7.0

    class _FakePsutil:
        @staticmethod
        def Process():
            calls.append(1)
            return _FakeProc()

    monkeypatch.setattr(dashboard, "psutil", _FakePsutil)
    assert dashboard._my_create_time() == 7.0
    assert dashboard._my_create_time() == 7.0
    assert len(calls) == 1  # cached — the process is probed once
    assert dashboard._MY_CREATE_TIME == 7.0

    # Without psutil the stamp is None but still latches as computed.
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME", None)
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME_SET", False)
    monkeypatch.setattr(dashboard, "psutil", None)
    assert dashboard._my_create_time() is None
    assert dashboard._MY_CREATE_TIME_SET is True

    # A psutil that blows up is suppressed to the same None stamp.
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME", None)
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME_SET", False)

    class _BoomPsutil:
        @staticmethod
        def Process():
            raise RuntimeError("nope")

    monkeypatch.setattr(dashboard, "psutil", _BoomPsutil)
    assert dashboard._my_create_time() is None
    assert dashboard._MY_CREATE_TIME_SET is True


def test_dash_claim_name_shape(monkeypatch):
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME", 12.5)
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME_SET", True)
    assert (
        dashboard._dash_claim_name("msg_1_abcd1234.json")
        == f"msg_1_abcd1234.json.{os.getpid()}.12.5.claim"
    )
    # No create time -> legacy pid-only shape.
    monkeypatch.setattr(dashboard_outbox, "_MY_CREATE_TIME", None)
    assert (
        dashboard._dash_claim_name("msg_1_abcd1234.json")
        == f"msg_1_abcd1234.json.{os.getpid()}.claim"
    )


def test_owner_alive_rejects_bad_pids():
    assert dashboard._owner_alive(0, None) is False
    assert dashboard._owner_alive(-3, None) is False
    assert dashboard._owner_alive("pid", None) is False
    assert dashboard._owner_alive(None, None) is False


def test_owner_alive_uses_dashboard_psutil(monkeypatch):
    """Monkeypatching ``dashboard.psutil`` to None drives the helper's
    fallback — proving the dependency is resolved at call time."""
    monkeypatch.setattr(dashboard, "psutil", None)
    alive = dashboard._owner_alive(os.getpid(), None)
    if os.name == "posix":
        assert alive is True  # kill(pid, 0) fallback
    else:
        assert alive is False  # fails closed without psutil


@pytest.mark.skipif(dashboard.psutil is None, reason="psutil not installed")
def test_owner_alive_with_real_psutil():
    assert dashboard._owner_alive(os.getpid(), None) is True
    # A wrong create time fails the identity check — a recycled pid never
    # passes for the recorded owner.
    assert dashboard._owner_alive(os.getpid(), 0.0) is False


def test_task_action_fields_owner_states():
    fields = dashboard._task_action_fields
    # In-flight row, no live owner -> resumable via dead-owner adoption and
    # flagged owner_lost.
    assert fields({"status": "running"}, set()) == (True, False, True)
    assert fields({"status": "queued"}, set()) == (True, False, True)
    # Terminal states: cancelled/failed resume unless the session is dead;
    # every other status is not resumable.
    assert fields({"status": "failed", "session_id": "s1"}, set()) == (True, False, False)
    assert fields({"status": "cancelled", "session_id": "s1"}, set()) == (True, False, False)
    assert fields({"status": "failed", "session_id": "s1"}, {"s1"}) == (False, False, False)
    assert fields({"status": "done", "session_id": "s1"}, set()) == (False, False, False)


@pytest.mark.skipif(dashboard.psutil is None, reason="psutil not installed")
def test_task_action_fields_live_owner():
    # A live sibling owner (this process stands in) makes an in-flight row
    # remote-owned: not resumable, not lost.
    row = {"status": "running", "owner_pid": os.getpid(), "owner_create_time": None}
    assert dashboard._task_action_fields(row, set()) == (False, True, False)


def test_req_and_msg_name_res():
    assert dashboard.MSG_NAME_RE.match("msg_1700000000000_abcd1234.json")
    assert not dashboard.MSG_NAME_RE.match("req_pause_1_abcd1234.json")
    assert dashboard.REQ_NAME_RE.match("req_pause_1700000000000_abcd1234.json")
    assert dashboard.REQ_NAME_RE.match("req_cancel_1_abcd1234.json")
    assert dashboard.REQ_NAME_RE.match("req_resume_1_abcd1234.json")
    assert not dashboard.REQ_NAME_RE.match("req_boom_1_abcd1234.json")
    assert not dashboard.REQ_NAME_RE.match("msg_1_abcd1234.json")
