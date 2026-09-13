"""Unit tests for per-run usage aggregation (agent_bridge.usage)."""

from __future__ import annotations

import math

from agent_bridge.usage import (
    DeltaUsage,
    RunUsage,
    normalize_usage,
    run_usage_from_total,
    usage_stream_key,
)


def devin_update(input_toks=None, output=None, *, run_id=None, parent=None, used=None, size=None):
    meta = {}
    if input_toks is not None:
        meta["cognition.ai/inputTokens"] = input_toks
    if output is not None:
        meta["cognition.ai/outputTokens"] = output
    if run_id or parent:
        ctx = {}
        if run_id:
            ctx["runId"] = run_id
        if parent:
            ctx["parentAgentId"] = parent
        meta["cognition.ai/subagent_context"] = ctx
    update = {"sessionUpdate": "usage_update"}
    if used is not None:
        update["used"] = used
    if size is not None:
        update["size"] = size
    if meta:
        update["_meta"] = meta
    return update


def test_cumulative_snapshots_delta_provider_neutral():
    run = RunUsage("echo")
    run.reset()
    run.update({"inputTokens": 10, "outputTokens": 4, "used": 5, "size": 100})
    snap = run.snapshot()
    assert snap["input"] == 10 and snap["output"] == 4 and snap["total"] == 14
    run.update({"inputTokens": 25, "outputTokens": 9, "used": 8, "size": 100})
    out = run.finish()
    assert out["input"] == 25
    assert out["output"] == 9
    assert out["total"] == 34
    assert out["quality"] == "exact"
    assert out["scope"] == "run"
    assert out["used"] == 8 and out["size"] == 100
    assert out["streams"] == 1


def test_reset_epoch_counts_new_absolute_value():
    run = RunUsage("echo")
    run.reset()
    run.update({"inputTokens": 100})
    run.update({"inputTokens": 40})  # counter reset (e.g. compaction)
    out = run.finish()
    assert out["input"] == 140
    assert out["total"] == 140


def test_devin_output_is_per_step_and_exact_dups_suppressed():
    run = RunUsage("devin")
    run.reset()
    run.update(devin_update(10, 4, used=5, size=100))
    # Paired re-emission: identical snapshot must not double-count output.
    run.update(devin_update(10, 4, used=5, size=100))
    run.update(devin_update(25, 9, used=8, size=100))
    out = run.finish()
    assert out["input"] == 25
    assert out["output"] == 13  # 4 + 9 summed, duplicate skipped
    assert out["total"] == 38


def test_non_devin_output_is_cumulative_snapshot():
    run = RunUsage("echo")
    run.reset()
    run.update(devin_update(10, 4))
    run.update(devin_update(25, 9))
    out = run.finish()
    assert out["output"] == 9
    assert out["total"] == 34


def test_subagent_streams_sum_and_do_not_leak_ids():
    run = RunUsage("devin")
    run.reset()
    run.update(devin_update(10, 4, used=5, size=100))
    run.update(devin_update(20, 1, run_id="sub-1", parent="root"))
    norm = run.update(devin_update(35, 3, run_id="sub-1", parent="root"))
    assert "stream" not in norm  # routing key never leaks into public events
    out = run.finish()
    # root: in 25? no — root saw only {in:10,out:4}; sub-1: in 20->35, out 1+3
    assert out["input"] == 45
    assert out["output"] == 8
    assert out["total"] == 53
    assert out["streams"] == 2


def test_parent_agent_id_groups_stream_when_no_run_id():
    assert usage_stream_key(devin_update(run_id="r1", parent="p1")) == "r1"
    assert usage_stream_key(devin_update(parent="p1")) == "p1"
    assert usage_stream_key(devin_update()) == ""
    assert usage_stream_key({"_meta": {"cognition.ai/subagent_context": {}}}) == ""


def test_context_window_used_size_never_counted():
    run = RunUsage("echo")
    run.reset()
    run.update({"used": 50, "size": 200})
    out = run.finish()
    assert out["used"] == 50 and out["size"] == 200
    assert "total" not in out and "input" not in out and "output" not in out


def test_prev_snapshots_survive_reset_so_only_turn_delta_counts():
    run = RunUsage("echo")
    run.reset()
    run.update({"inputTokens": 10})
    run.finish()
    run.reset()
    run.update({"inputTokens": 30})
    out = run.finish()
    assert out["input"] == 20


def test_prompt_response_fallback_first_turn_counts_full():
    run = RunUsage("echo")
    run.reset(resumed=False)
    run.note_conversation_snapshot({"totalTokens": 3, "inputTokens": 1, "outputTokens": 2})
    out = run.finish()
    assert out["input"] == 1 and out["output"] == 2 and out["total"] == 3
    assert out["quality"] == "exact"


def test_prompt_response_fallback_deltas_against_previous_turn():
    run = RunUsage("echo")
    run.reset()
    run.note_conversation_snapshot({"totalTokens": 3, "inputTokens": 1, "outputTokens": 2})
    run.finish()
    run.reset(resumed=True)
    run.note_conversation_snapshot({"totalTokens": 10, "inputTokens": 6, "outputTokens": 4})
    out = run.finish()
    assert out["input"] == 5 and out["output"] == 2 and out["total"] == 7
    assert out["quality"] == "exact"


def test_resumed_without_baseline_marks_estimate_and_keeps_conversation_total():
    run = RunUsage("echo")
    run.reset(resumed=True)
    run.note_conversation_snapshot({"totalTokens": 150, "inputTokens": 100, "outputTokens": 50})
    out = run.finish()
    assert out["quality"] == "estimate"
    assert "input" not in out and "output" not in out and "total" not in out
    assert out["conversation_total"] == {"input": 100, "output": 50, "total": 150}


def test_resumed_with_persisted_baseline_deltas():
    run = RunUsage("echo")
    run.reset(resumed=True, conv_baseline={"input": 100, "output": 50, "total": 150})
    run.note_conversation_snapshot({"totalTokens": 200, "inputTokens": 120, "outputTokens": 80})
    out = run.finish()
    assert out["input"] == 20 and out["output"] == 30 and out["total"] == 50
    assert out["quality"] == "exact"


def test_stream_counters_win_over_prompt_response_snapshot():
    run = RunUsage("echo")
    run.reset()
    run.update({"inputTokens": 10})
    run.note_conversation_snapshot({"inputTokens": 999, "outputTokens": 999})
    out = run.finish()
    assert out["input"] == 10
    assert "999" not in repr(out)
    # Baseline still commits so the next turn's delta is correct.
    assert run.conversation_baseline["input"] == 999


def test_normalize_usage_picks_and_guards():
    norm = normalize_usage(
        {
            "used": 7,
            "size": 128,
            "_meta": {"cognition.ai/inputTokens": 5, "cognition.ai/subagent_context": {"runId": "r"}},
        }
    )
    assert norm["input"] == 5 and norm["used"] == 7 and norm["size"] == 128
    assert norm["stream"] == "r"
    assert normalize_usage("nope") == {}
    assert normalize_usage({"inputTokens": math.nan, "outputTokens": -1}) == {
        "stream": ""
    }
    assert normalize_usage({"inputTokens": True})["stream"] == ""  # bool is not a counter


def test_delta_usage_fresh_turn_counts_first_sighting():
    d = DeltaUsage(count_first=True)
    d.update({"input_tokens": 10, "output_tokens": 4})
    d.update({"input_tokens": 15, "output_tokens": 9})
    out = d.run_usage()
    assert out["input"] == 15 and out["output"] == 9 and out["total"] == 24
    assert out["quality"] == "exact"


def test_delta_usage_resumed_with_baseline_counts_only_delta():
    d = DeltaUsage(baseline={"input": 100, "output": 50})
    d.update({"input_tokens": 130, "output_tokens": 60})
    out = d.run_usage(resumed=True)
    assert out["input"] == 30 and out["output"] == 10 and out["total"] == 40
    assert out["quality"] == "exact"
    assert out["conversation_total"] == {"input": 130, "output": 60}


def test_delta_usage_resumed_without_baseline_is_estimate():
    d = DeltaUsage(baseline={})
    d.update({"input_tokens": 500, "output_tokens": 100})
    d.update({"input_tokens": 560, "output_tokens": 130})
    out = d.run_usage(resumed=True)
    # First sighting set the boundary; only the later delta is attributable.
    assert out["input"] == 60 and out["output"] == 30
    assert out["quality"] == "estimate"
    assert out["conversation_total"]["input"] == 560


def test_delta_usage_reset_epoch():
    d = DeltaUsage(count_first=True)
    d.update({"input_tokens": 200})
    d.update({"input_tokens": 50})
    out = d.run_usage()
    assert out["input"] == 250


def test_run_usage_from_total_codex_shape():
    out = run_usage_from_total(
        {
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "output_tokens": 4,
            "reasoning_output_tokens": 1,
        }
    )
    assert out["input"] == 10
    assert out["cached_read"] == 0 or "cached_read" not in out
    assert out["output"] == 4
    assert out["total"] == 14
    assert out["quality"] == "exact" and out["scope"] == "run"
    # reasoning output is already inside output — never added on top
    assert "reasoning_output_tokens" not in out


def test_run_usage_from_total_empty_is_marker_only():
    out = run_usage_from_total({})
    assert out == {"scope": "run", "quality": "exact"}
