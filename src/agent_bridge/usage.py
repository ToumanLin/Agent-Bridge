"""Aggregate provider usage reports into per-run token consumption.

A "run" is exactly one Task / one worker turn — from task.started_at through
the terminal task.finished_at. ``run_usage`` records accumulated consumption
for the whole run: the root agent plus any subagent usage streams.

Provider semantics differ:

- Devin (``devin acp``): ``UsageUpdate._meta`` input / cached counters are
  cumulative snapshots (they reset on context compaction — a decrease starts
  a new epoch), while ``outputTokens`` is a per-step count that must be
  summed. Subagent usage arrives as separate streams identified by
  ``cognition.ai/subagent_context`` (``runId`` when present, else
  ``parentAgentId``; absent means the root stream).
- Other ACP workers: counters in ``UsageUpdate`` fields/``_meta`` are treated
  as cumulative snapshots — the provider-neutral safe choice.
  ``PromptResponse.usage`` is documented as cumulative across all turns of a
  session, so it is reconciled as a conversation-cumulative snapshot (delta
  against the previous turn's snapshot) and only used when no ``UsageUpdate``
  counters arrived.
- Codex ``turn.completed.usage`` is already a per-turn total.
- agy ``usage`` is ``scope="turn"`` (per-run) on fresh conversations and
  ``scope="conversation"`` (covers every prior turn) when resumed; the run
  delta is derived against the counters persisted after the previous turn.

The headline ``total`` is ``input + output`` — conventional token accounting
where cached reads are part of input. ``used`` / ``size`` are context-window
occupancy snapshots, carried as metadata only and never summed.
"""

from __future__ import annotations

import math
from typing import Any

COUNTER_KEYS = ("input", "cached_read", "cached_write", "output", "total")
_CONTEXT_KEYS = ("used", "size")
_COST_KEYS = ("credit_cost", "acu_cost")

_PICKS: dict[str, tuple[str, ...]] = {
    "input": ("input", "input_tokens", "inputTokens", "cognition.ai/inputTokens"),
    "cached_read": (
        "cached_read",
        "cached_input_tokens",
        "cachedInputTokens",
        "cachedReadTokens",
        "cognition.ai/cachedReadTokens",
    ),
    "cached_write": (
        "cached_write",
        "cached_write_tokens",
        "cachedWriteTokens",
        "cognition.ai/cachedWriteTokens",
    ),
    "output": ("output", "output_tokens", "outputTokens", "cognition.ai/outputTokens"),
    "total": ("total", "total_tokens", "totalTokens", "cognition.ai/totalTokens"),
    "used": ("used",),
    "size": ("size",),
    "credit_cost": ("credit_cost", "totalCreditCost", "cognition.ai/totalCreditCost"),
    "acu_cost": ("acu_cost", "totalAcuCost", "cognition.ai/totalAcuCost"),
}

# Agents whose UsageUpdate output counter is per-step (summed) rather than a
# cumulative snapshot. Everything else gets provider-neutral monotonic deltas.
_PER_STEP_OUTPUT_AGENTS = frozenset({"devin"})


def _num(value: Any) -> int | float | None:
    """Return a finite non-negative counter value, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value) if float(value).is_integer() else value


def usage_stream_key(raw: Any) -> str:
    """Identify the subagent usage stream; ``""`` is the root agent."""
    if not isinstance(raw, dict):
        return ""
    meta = raw.get("_meta")
    if not isinstance(meta, dict):
        return ""
    ctx = meta.get("cognition.ai/subagent_context")
    if not isinstance(ctx, dict):
        return ""
    for key in ("runId", "run_id", "parentAgentId", "parent_agent_id"):
        value = ctx.get(key)
        if isinstance(value, str) and value:
            return value
        if _num(value) is not None:
            return str(value)
    return ""


def normalize_usage(raw: Any) -> dict[str, Any]:
    """Flatten one provider usage payload into canonical keys.

    Top-level fields are read first, then the ACP ``_meta`` bag (Devin's
    ``cognition.ai/*`` counters). Only known keys survive — provider metadata
    such as the subagent id itself never leaks into the result beyond the
    internal ``stream`` routing key.
    """
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    meta = raw.get("_meta")
    sources = (raw, meta if isinstance(meta, dict) else {})
    for key, names in _PICKS.items():
        for name in names:
            for src in sources:
                value = _num(src.get(name))
                if value is not None:
                    out[key] = value
                    break
            if key in out:
                break
    cost = raw.get("cost")
    if isinstance(cost, dict):
        out["cost"] = {k: v for k, v in cost.items() if isinstance(k, str)}
    elif _num(cost) is not None:
        out["cost"] = cost
    scope = raw.get("scope")
    if isinstance(scope, str) and scope:
        out["scope"] = scope
    out["stream"] = usage_stream_key(raw)
    return out


def _public_update(norm: dict[str, Any]) -> dict[str, Any]:
    """Normalized update without the stream routing key (hides subagent ids)."""
    return {k: v for k, v in norm.items() if k != "stream"}


def _finish_total(out: dict[str, Any]) -> None:
    """Headline = input + output (input already includes cached reads)."""
    if "input" in out or "output" in out:
        out["total"] = int(out.get("input", 0)) + int(out.get("output", 0))


def _intish(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


class RunUsage:
    """Accumulates one run's token consumption across ACP usage streams.

    ``prev`` counter snapshots deliberately survive ``reset`` — a counter that
    is cumulative over the agent process's lifetime contributes only its delta
    to each run, while ``consumed`` is per run. A decrease is a reset epoch
    (context compaction): the new absolute value counts in full.
    """

    def __init__(self, agent: str = "") -> None:
        self.agent = agent
        self._streams: dict[str, dict[str, Any]] = {}
        self._conv_prev: dict[str, float] = {}
        self._conv_latest: dict[str, float] | None = None
        self.resumed = False
        self.used: int | float | None = None
        self.size: int | float | None = None
        self.cost: Any = None
        self.credit_cost: int | float | None = None
        self.acu_cost: int | float | None = None

    def reset(
        self,
        *,
        resumed: bool = False,
        conv_baseline: dict[str, Any] | None = None,
    ) -> None:
        """Start a new run. ``conv_baseline`` is the persisted conversation
        snapshot from a previous bridge process (restart-safe fallback)."""
        if self._conv_latest is not None:
            # A snapshot seen but never finalized (turn raised) still commits —
            # otherwise the next run's delta would span two turns.
            self._conv_prev = dict(self._conv_latest)
        if not self._conv_prev and conv_baseline:
            self._conv_prev = {
                k: v for k, v in conv_baseline.items() if _num(v) is not None
            }
        for st in self._streams.values():
            st["consumed"] = {}
            st["fingerprint"] = None
            st["seen"] = False
        self.resumed = resumed
        self.used = None
        self.size = None
        self.cost = None
        self.credit_cost = None
        self.acu_cost = None
        self._conv_latest = None

    @property
    def conversation_baseline(self) -> dict[str, float]:
        return dict(self._conv_prev)

    def update(self, raw: Any) -> dict[str, Any]:
        """Fold one UsageUpdate payload in; returns the normalized update."""
        norm = normalize_usage(raw)
        for key in (*_CONTEXT_KEYS, *_COST_KEYS):
            value = norm.get(key)
            if value is not None:
                setattr(self, key, value)
        if norm.get("cost") is not None:
            self.cost = norm["cost"]
        counters = {k: norm[k] for k in COUNTER_KEYS if k in norm}
        if not counters:
            return _public_update(norm)
        stream = self._streams.setdefault(
            str(norm.get("stream") or ""),
            {"prev": {}, "consumed": {}, "fingerprint": None, "seen": False},
        )
        stream["seen"] = True
        fingerprint = tuple((k, norm.get(k)) for k in (*COUNTER_KEYS, *_CONTEXT_KEYS))
        if fingerprint == stream["fingerprint"]:
            # Exact re-emission of the previous snapshot: cumulative keys would
            # delta to 0 anyway; per-step keys must not re-add.
            return _public_update(norm)
        stream["fingerprint"] = fingerprint
        for key, value in counters.items():
            if key == "output" and self.agent in _PER_STEP_OUTPUT_AGENTS:
                stream["consumed"][key] = stream["consumed"].get(key, 0) + value
                continue
            prev = stream["prev"].get(key)
            stream["prev"][key] = value
            delta = value if prev is None or value < prev else value - prev
            stream["consumed"][key] = stream["consumed"].get(key, 0) + delta
        return _public_update(norm)

    def note_conversation_snapshot(self, raw: Any) -> None:
        """Record a conversation-cumulative snapshot (PromptResponse.usage)."""
        norm = normalize_usage(raw)
        counters = {k: norm[k] for k in COUNTER_KEYS if k in norm}
        if counters:
            self._conv_latest = counters

    def _stream_consumed(self) -> dict[str, float]:
        consumed: dict[str, float] = {}
        for st in self._streams.values():
            for key, value in st["consumed"].items():
                consumed[key] = consumed.get(key, 0) + value
        return consumed

    def snapshot(self) -> dict[str, Any]:
        """Live partial: stream counters so far plus context metadata."""
        out: dict[str, Any] = {"scope": "run", "quality": "exact"}
        for key, value in self._stream_consumed().items():
            if value:
                out[key] = _intish(value)
        streams = sum(1 for st in self._streams.values() if st["seen"])
        if streams:
            out["streams"] = streams
        _finish_total(out)
        if self.used is not None:
            out["used"] = self.used
        if self.size is not None:
            out["size"] = self.size
        if self.cost is not None:
            out["cost"] = self.cost
        if self.credit_cost is not None:
            out["credit_cost"] = self.credit_cost
        if self.acu_cost is not None:
            out["acu_cost"] = self.acu_cost
        return out

    def finish(self) -> dict[str, Any]:
        """Final run_usage for the task record.

        When no UsageUpdate counters arrived, the conversation-cumulative
        PromptResponse.usage snapshot (delta against the previous turn's
        snapshot) is the fallback. On a resumed session without a baseline the
        delta cannot be attributed to this run — those keys contribute 0, the
        result is ``quality="estimate"``, and the whole-conversation counters
        stay separate under ``conversation_total``.
        """
        out = self.snapshot()
        if not any(out.get(k) for k in COUNTER_KEYS) and self._conv_latest:
            unbased = False
            for key, value in self._conv_latest.items():
                prev = self._conv_prev.get(key)
                if prev is None:
                    if self.resumed:
                        unbased = True
                        continue
                    delta = value
                elif value < prev:
                    delta = value
                else:
                    delta = value - prev
                if delta:
                    out[key] = _intish(delta)
            if unbased:
                out["quality"] = "estimate"
                out["conversation_total"] = dict(self._conv_latest)
            _finish_total(out)
        if self._conv_latest is not None:
            self._conv_prev = dict(self._conv_latest)
        return out


class DeltaUsage:
    """Monotonic-delta accumulation over one usage channel (agy).

    ``count_first`` is for ``scope="turn"`` payloads — the first sighting of
    each key counts in full. For ``scope="conversation"`` payloads (resumed
    runs) ``baseline`` carries the counters persisted after the previous turn
    so only this run's increment is counted; without a baseline the first
    sighting establishes the attribution boundary — it contributes 0, later
    deltas count, and the result is ``quality="estimate"``.
    """

    def __init__(
        self,
        baseline: dict[str, Any] | None = None,
        *,
        count_first: bool = False,
    ) -> None:
        self.prev: dict[str, float] = {
            k: v for k, v in (baseline or {}).items() if _num(v) is not None
        }
        self.count_first = count_first
        self.consumed: dict[str, float] = {}
        self.latest: dict[str, float] = {}
        self.unbased: set[str] = set()
        self.used: int | float | None = None
        self.size: int | float | None = None
        self.cost: Any = None
        self.credit_cost: int | float | None = None
        self.acu_cost: int | float | None = None
        self.seen = False

    def update(self, raw: Any) -> None:
        norm = normalize_usage(raw)
        counters = {k: norm[k] for k in COUNTER_KEYS if k in norm}
        for key in (*_CONTEXT_KEYS, *_COST_KEYS):
            value = norm.get(key)
            if value is not None:
                setattr(self, key, value)
        if norm.get("cost") is not None:
            self.cost = norm["cost"]
        if not counters:
            return
        self.seen = True
        for key, value in counters.items():
            self.latest[key] = value
            prev = self.prev.get(key)
            if prev is None:
                if self.count_first:
                    self.consumed[key] = self.consumed.get(key, 0) + value
                else:
                    self.unbased.add(key)
            else:
                self.consumed[key] = self.consumed.get(key, 0) + (
                    value if value < prev else value - prev
                )
            self.prev[key] = value

    def run_usage(self, *, resumed: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "scope": "run",
            "quality": "estimate" if self.unbased else "exact",
        }
        for key, value in self.consumed.items():
            if value:
                out[key] = _intish(value)
        _finish_total(out)
        if resumed and self.latest:
            out["conversation_total"] = dict(self.latest)
        if self.used is not None:
            out["used"] = self.used
        if self.size is not None:
            out["size"] = self.size
        if self.cost is not None:
            out["cost"] = self.cost
        if self.credit_cost is not None:
            out["credit_cost"] = self.credit_cost
        if self.acu_cost is not None:
            out["acu_cost"] = self.acu_cost
        return out


def run_usage_from_total(raw: Any) -> dict[str, Any]:
    """run_usage for providers that emit one authoritative per-run total
    (Codex ``turn.completed.usage``)."""
    norm = normalize_usage(raw)
    out: dict[str, Any] = {"scope": "run", "quality": "exact"}
    for key in (*COUNTER_KEYS, *_CONTEXT_KEYS, *_COST_KEYS):
        if key in norm:
            out[key] = norm[key]
    if "cost" in norm:
        out["cost"] = norm["cost"]
    _finish_total(out)
    return out
