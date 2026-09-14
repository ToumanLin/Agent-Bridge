from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_bridge.config import AgentConfig, AppConfig, QuotaConfig, load_config
from agent_bridge.quota import (
    FAILURE_CACHE_SEC,
    QuotaBalance,
    QuotaCache,
    QuotaProvider,
    QuotaStatus,
    QuotaWindow,
    _proxy_map,
    classify_error,
    default_providers,
    describe_quotas,
    fetch_quota,
    looks_like_config_error,
    looks_like_quota_error,
    looks_like_transient_error,
    parse_timestamp,
    provider_table,
    resolve_provider,
    summarize_quota,
    unknown_quota,
    window_from_reset,
    window_name_from_minutes,
)
from agent_bridge.quota_claude import claude_access_token, fetch_claude_quota, parse_claude_usage
from agent_bridge.quota_codex import fetch_codex_quota, parse_codex_rate_limits
from agent_bridge.quota_grok import fetch_grok_quota, grok_auth_headers, parse_grok_billing
from agent_bridge.quota_kimi import fetch_kimi_quota, kimi_access_token, kimi_usage_url, parse_kimi_usage


@pytest.fixture(autouse=True)
def isolated_quota_homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("KIMI_CODE_HOME", "GROK_HOME"):
        monkeypatch.delenv(key, raising=False)


FAKE_CODEX = Path(__file__).with_name("fake_codex.py")
FAR_FUTURE = 4102444800  # 2100-01-01T00:00:00Z


def _agent(name: str = "fake", protocol: str = "fake") -> AgentConfig:
    return AgentConfig(name=name, protocol=protocol, command=[name])


# --- shared helpers -----------------------------------------------------------


def test_parse_timestamp_accepts_epoch_seconds_millis_and_iso():
    seconds = parse_timestamp(FAR_FUTURE)
    millis = parse_timestamp(FAR_FUTURE * 1000)
    iso_z = parse_timestamp("2100-01-01T00:00:00Z")
    iso_nanos = parse_timestamp("2100-01-01T00:00:00.123456789Z")
    numeric_text = parse_timestamp(str(FAR_FUTURE))
    assert seconds == millis == iso_z == numeric_text == datetime(2100, 1, 1, tzinfo=UTC)
    assert iso_nanos is not None and iso_nanos.microsecond == 123456
    assert parse_timestamp(None) is None
    assert parse_timestamp(True) is None
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(0) is None


def test_window_from_reset_reports_seconds_until_reset():
    now = datetime(2099, 12, 31, 23, 0, tzinfo=UTC)
    window = window_from_reset("5h", 88.0, FAR_FUTURE, now=now)
    assert window.resets_at == "2100-01-01T00:00:00+00:00"
    assert window.resets_in_sec == 3600
    past = window_from_reset("5h", 88.0, "2000-01-01T00:00:00Z", now=now)
    assert past.resets_in_sec == 0
    assert window_from_reset("5h", None, None).resets_at is None


def test_window_name_from_minutes():
    assert window_name_from_minutes(300, "x") == "5h"
    assert window_name_from_minutes(10080, "x") == "weekly"
    assert window_name_from_minutes(1440, "x") == "daily"
    assert window_name_from_minutes(2880, "x") == "2d"
    assert window_name_from_minutes(120, "x") == "2h"
    assert window_name_from_minutes(45, "x") == "45m"
    assert window_name_from_minutes(None, "fallback") == "fallback"


def test_summarize_quota_marks_exhausted_and_refuses_empty_answers():
    ok = summarize_quota([QuotaWindow(name="5h", remaining_percent=40.0)], source="s")
    assert ok.status == "ok" and ok.source == "s" and ok.fetched_at
    dry = summarize_quota([QuotaWindow(name="5h", remaining_percent=0.0)])
    assert dry.status == "exhausted"
    flagged = summarize_quota([], balance=QuotaBalance(amount="1"), exhausted=True)
    assert flagged.status == "exhausted"
    empty = summarize_quota([], detail="nothing here")
    assert empty.status == "unknown" and empty.detail == "nothing here"


@pytest.mark.parametrize(
    ("parse", "payload"),
    [
        (parse_codex_rate_limits, {"rateLimits": {"primary": {}}}),
        (parse_kimi_usage, {"usage": {}}),
        (parse_claude_usage, {"five_hour": {}}),
        (parse_kimi_usage, {"usage": {"reset_at": "2030-01-01T00:00:00Z"}}),
    ],
)
def test_empty_quota_windows_are_unknown(parse, payload):
    status = parse(payload)
    assert status.status == "unknown"
    assert status.detail


def test_partial_quota_keeps_known_windows_balance_and_explicit_exhaustion():
    missing = QuotaWindow(name="5h")
    assert summarize_quota([missing, QuotaWindow(name="weekly", remaining_percent=40)]).status == "ok"
    assert summarize_quota([missing, QuotaWindow(name="weekly", remaining_percent=0)]).status == "exhausted"
    assert summarize_quota([missing], balance=QuotaBalance(amount="unlimited")).status == "ok"
    assert summarize_quota([missing], exhausted=True).status == "exhausted"


def test_looks_like_quota_error():
    assert looks_like_quota_error("quota exceeded")
    assert looks_like_quota_error("HTTP 429 Rate limit reached")
    assert looks_like_quota_error("Insufficient Balance")
    assert not looks_like_quota_error("bridge_restarted")
    assert not looks_like_quota_error(None)


AGY_CAPACITY_503 = (
    "API error (attempt 1): UNAVAILABLE (code 503): "
    "No capacity available for model gemini-3.8-flash-medium on the server"
)
AGY_EFFORT_CONFLICT = (
    'invalid model selection (--model "gemini-3.8-flash-medium" --effort "high"): '
    "--model gemini-3.8-flash-medium conflicts with --effort=high"
)


def test_looks_like_transient_error_matches_real_503_capacity_strings():
    assert looks_like_transient_error(AGY_CAPACITY_503)
    # The upstream JSON payload the stream text is wrapped from.
    assert looks_like_transient_error(
        '{"code": 503, "status": "UNAVAILABLE", "reason": "MODEL_CAPACITY_EXHAUSTED",'
        ' "message": "No capacity available for model x on the server"}'
    )
    assert looks_like_transient_error("The model is overloaded right now")
    assert looks_like_transient_error("gRPC DEADLINE_EXCEEDED / deadline exceeded")
    # Auth, config, and quota text must not classify as transient.
    for text in (
        "authentication required",
        AGY_EFFORT_CONFLICT,
        "unknown model foo",
        "quota exceeded",
        "HTTP 429 Rate limit reached",
        "bridge_restarted",
        "",
        None,
    ):
        assert not looks_like_transient_error(text)


def test_capacity_503_is_not_quota():
    # Guards against misclassifying provider capacity as plan exhaustion.
    assert not looks_like_quota_error(AGY_CAPACITY_503)
    # "deadline exceeded" trips the "exceeded" quota marker but is transient.
    assert looks_like_quota_error("deadline exceeded")
    assert looks_like_transient_error("deadline exceeded")


def test_looks_like_config_error():
    assert looks_like_config_error(AGY_EFFORT_CONFLICT)
    assert looks_like_config_error("unknown model foo-9")
    assert not looks_like_config_error(AGY_CAPACITY_503)
    assert not looks_like_config_error("authentication required")
    assert not looks_like_config_error(None)


def test_classify_error_kinds():
    assert classify_error(AGY_CAPACITY_503) == "transient_provider"
    assert classify_error("deadline exceeded") == "transient_provider"
    assert classify_error("quota exceeded") == "quota"
    assert classify_error(AGY_EFFORT_CONFLICT) == "config"
    assert classify_error("authentication required") == "unknown"
    assert classify_error(None) == "unknown"


def test_proxy_map_prefers_upper_case_and_skips_blank():
    env = {"HTTPS_PROXY": "http://p:1", "https_proxy": "http://ignored", "http_proxy": " ", "HTTP_PROXY": ""}
    assert _proxy_map(env) == {"https": "http://p:1"}
    assert _proxy_map({}) == {}
    assert _proxy_map({"ALL_PROXY": "http://all:1", "https_proxy": "http://https:2"}) == {
        "http": "http://all:1", "https": "http://https:2",
    }
    assert _proxy_map({"all_proxy": "http://all:1"}) == {"http": "http://all:1", "https": "http://all:1"}


# --- cache ---------------------------------------------------------------------


def test_quota_cache_expires_and_keeps_last_good():
    cache = QuotaCache(ttl_sec=10)
    good = QuotaStatus(status="ok", windows=[QuotaWindow(name="5h", remaining_percent=50)])
    cache.put("codex", good)
    now = time.monotonic()
    assert cache.get("codex", now=now) is good
    assert cache.get("codex", now=now + 11) is None
    assert cache.last_good("codex") is good
    cache.put("codex", unknown_quota("boom"))
    assert cache.get("codex").status == "unknown"
    assert cache.last_good("codex") is good
    cache.invalidate("codex")
    assert cache.get("codex") is None
    assert cache.last_good("codex") is good
    cache.clear()
    assert cache.last_good("codex") is None


def test_quota_cache_zero_ttl_never_serves_fresh():
    cache = QuotaCache(ttl_sec=0)
    cache.put("x", QuotaStatus(status="ok"))
    assert cache.get("x") is None
    assert cache.last_good("x") is not None


# --- fetch_quota --------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_quota_unknown_when_no_provider():
    cache = QuotaCache(60)
    row = await fetch_quota(_agent("mystery", "acp"), {}, cache=cache, timeout_sec=1, providers={})
    assert row["status"] == "unknown"
    assert "not supported for mystery" in row["detail"]
    assert row["cached"] is False
    again = await fetch_quota(_agent("mystery", "acp"), {}, cache=cache, timeout_sec=1, providers={})
    assert again["cached"] is True


@pytest.mark.asyncio
async def test_fetch_quota_serves_cache_and_marks_it():
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        return QuotaStatus(status="ok", windows=[QuotaWindow(name="5h", remaining_percent=70)])

    cache = QuotaCache(60)
    cfg = _agent("kimi", "acp")
    first = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"kimi": QuotaProvider(None, provider)})
    second = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"kimi": QuotaProvider(None, provider)})
    assert calls == 1
    assert first["cached"] is False and second["cached"] is True
    assert second["windows"][0]["remaining_percent"] == 70
    assert first["fetched_at"]


@pytest.mark.asyncio
async def test_fetch_quota_timeout_is_unknown_and_bounded():
    async def slow(cfg, env):
        await asyncio.sleep(5)
        return QuotaStatus(status="ok")

    cache = QuotaCache(600)
    started = time.monotonic()
    row = await fetch_quota(_agent("grok", "acp"), {}, cache=cache, timeout_sec=0.2, providers={"grok": QuotaProvider(None, slow)})
    assert time.monotonic() - started < 2
    assert row["status"] == "unknown"
    assert "timed out after 0.2s" in row["detail"]
    # Failures are memoised for a short spell only.
    assert cache.get("grok") is not None
    assert cache.get("grok", now=time.monotonic() + FAILURE_CACHE_SEC + 1) is None


@pytest.mark.asyncio
async def test_fetch_quota_provider_exception_falls_back_to_last_good():
    state = {"fail": False}

    async def flaky(cfg, env):
        if state["fail"]:
            raise RuntimeError("HTTP 429: Rate limited.")
        return QuotaStatus(status="ok", windows=[QuotaWindow(name="weekly", remaining_percent=61)])

    cache = QuotaCache(600)
    cfg = _agent("claude", "acp")
    good = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"claude": QuotaProvider("test source", flaky)})
    assert good["status"] == "ok" and good["source"] == "test source"
    cache.invalidate("claude")
    state["fail"] = True
    stale = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"claude": QuotaProvider("test source", flaky)})
    assert stale["status"] == "ok"
    assert stale["stale"] is True and stale["cached"] is True
    assert "RuntimeError: HTTP 429" in stale["detail"]
    assert stale["windows"][0]["remaining_percent"] == 61


@pytest.mark.asyncio
async def test_fetch_quota_provider_exception_without_history_is_unknown():
    async def broken(cfg, env):
        raise ValueError("bad payload")

    row = await fetch_quota(_agent("dsh", "acp"), {}, cache=QuotaCache(60), timeout_sec=1, providers={"dsh": QuotaProvider(None, broken)})
    assert row["status"] == "unknown"
    assert "ValueError: bad payload" in row["detail"]


@pytest.mark.asyncio
async def test_fetch_quota_normalises_bogus_status_and_fills_source():
    async def odd(cfg, env):
        return QuotaStatus(status="green", windows=[QuotaWindow(name="5h", remaining_percent=1)])

    row = await fetch_quota(_agent("kimi", "acp"), {}, cache=QuotaCache(60), timeout_sec=1, providers={"kimi": QuotaProvider("odd source", odd)})
    assert row["status"] == "unknown"
    assert row["source"] == "odd source"


def test_resolve_provider_prefers_name_then_protocol():
    table = {"kimi": "by-name", "protocol:codex": "by-protocol"}
    assert resolve_provider(_agent("kimi", "acp"), table) == "by-name"
    assert resolve_provider(_agent("codex-alt", "codex"), table) == "by-protocol"
    assert resolve_provider(_agent("cursor", "acp"), table) is None


def test_provider_table_gates_experimental_workers():
    plain = default_providers()
    assert set(plain) == {"protocol:codex", "kimi"}
    full = default_providers(experimental=True)
    assert {"grok", "claude"} <= set(full)
    config = AppConfig(quota=QuotaConfig(experimental=False))
    table = provider_table(config)
    assert table["grok"] is not full["grok"]
    assert provider_table(AppConfig(quota=QuotaConfig(experimental=True)))["grok"] == full["grok"]


@pytest.mark.asyncio
async def test_experimental_placeholders_explain_the_flag():
    table = provider_table(AppConfig(quota=QuotaConfig(experimental=False)))
    grok = await table["grok"].fetch(_agent("grok", "acp"), {})
    claude = await table["claude"].fetch(_agent("claude", "acp"), {})
    for status in (grok, claude):
        assert status.status == "unknown"
        assert "experimental = true" in (status.detail or "")


# --- codex ---------------------------------------------------------------------


def test_parse_codex_rate_limits_windows_plan_and_credits():
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": FAR_FUTURE},
            "secondary": {"usedPercent": 2.5, "windowDurationMins": 10080, "resetsAt": FAR_FUTURE},
            "credits": {"hasCredits": True, "unlimited": False, "balance": "12.50"},
            "planType": "plus",
            "rateLimitReachedType": None,
        }
    }
    status = parse_codex_rate_limits(payload)
    assert status.status == "ok"
    assert [(w.name, w.remaining_percent) for w in status.windows] == [("5h", 90.0), ("weekly", 97.5)]
    assert status.windows[0].resets_at == "2100-01-01T00:00:00+00:00"
    assert status.windows[0].resets_in_sec is not None and status.windows[0].resets_in_sec > 0
    assert status.plan == "plus"
    assert status.balance == QuotaBalance(amount="12.50", currency="USD")


def test_parse_codex_rate_limits_reached_is_exhausted():
    payload = {"rateLimits": {"primary": {"usedPercent": 100}, "rateLimitReachedType": "primary"}}
    status = parse_codex_rate_limits(payload)
    assert status.status == "exhausted"
    assert "primary" in (status.detail or "")


def test_parse_codex_rate_limits_without_plan_is_unknown():
    assert parse_codex_rate_limits({}).status == "unknown"
    assert "API-key" in (parse_codex_rate_limits({}).detail or "")
    assert parse_codex_rate_limits(None).status == "unknown"
    unlimited = parse_codex_rate_limits({"rateLimits": {"credits": {"hasCredits": True, "unlimited": True}}})
    assert unlimited.status == "ok" and unlimited.balance == QuotaBalance(amount="unlimited")


def _codex_cfg() -> AgentConfig:
    return AgentConfig(name="codex", protocol="codex", command=["codex"])


def _patch_codex_command(monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.quota_codex.resolve_codex_command",
        lambda command, fallbacks=None, *, env=None: [sys.executable, str(FAKE_CODEX)],
    )


@pytest.mark.asyncio
async def test_codex_provider_reads_rate_limits_from_app_server(monkeypatch):
    _patch_codex_command(monkeypatch)
    monkeypatch.setattr("agent_bridge.quota_codex.POST_INIT_SETTLE_SEC", 0.0)
    status = await fetch_codex_quota(_codex_cfg(), {"PATH": "/usr/bin"})
    assert status.status == "ok"
    assert status.plan == "plus"
    assert [w.name for w in status.windows] == ["5h", "weekly"]
    assert status.windows[0].remaining_percent == 90.0


@pytest.mark.asyncio
async def test_codex_provider_reports_sign_in_errors(monkeypatch):
    _patch_codex_command(monkeypatch)
    monkeypatch.setattr("agent_bridge.quota_codex.POST_INIT_SETTLE_SEC", 0.0)
    env = {"FAKE_CODEX_APP_SERVER_ERROR": "token_invalidated"}
    with pytest.raises(RuntimeError, match="not signed in"):
        await fetch_codex_quota(_codex_cfg(), env)


@pytest.mark.asyncio
async def test_codex_provider_hang_becomes_unknown_through_fetch_quota(monkeypatch):
    _patch_codex_command(monkeypatch)
    env = {"FAKE_CODEX_APP_SERVER_HANG": "5"}
    started = time.monotonic()
    row = await fetch_quota(
        _codex_cfg(),
        env,
        cache=QuotaCache(60),
        timeout_sec=0.5,
        providers={"protocol:codex": QuotaProvider(None, fetch_codex_quota)},
    )
    assert time.monotonic() - started < 4
    assert row["status"] == "unknown"
    assert "timed out" in row["detail"]


# --- kimi ----------------------------------------------------------------------


def test_kimi_usage_url_honours_base_url_override():
    assert kimi_usage_url({}) == "https://api.kimi.com/coding/v1/usages"
    with pytest.raises(ValueError, match="custom endpoints"):
        kimi_usage_url({"KIMI_CODE_BASE_URL": "https://proxy.example/v1/"})


def test_kimi_access_token_reads_credential_file(tmp_path: Path):
    creds = tmp_path / "credentials"
    creds.mkdir()
    token, problem = kimi_access_token(tmp_path)
    assert token is None and "kimi login" in problem
    (creds / "kimi-code.json").write_text(
        json.dumps({"access_token": "tok", "refresh_token": "r", "expires_at": FAR_FUTURE}),
        encoding="utf-8",
    )
    assert kimi_access_token(tmp_path) == ("tok", None)
    (creds / "kimi-code.json").write_text(json.dumps({"access_token": "tok", "expires_at": 1}), encoding="utf-8")
    token, problem = kimi_access_token(tmp_path, now=100.0)
    assert token is None and "expired" in problem
    (creds / "kimi-code.json").write_text("{not json", encoding="utf-8")
    token, problem = kimi_access_token(tmp_path)
    assert token is None and "JSONDecodeError" in problem


def test_parse_kimi_usage_reads_summary_and_limits():
    payload = {
        "usage": {"used": 30, "limit": 100, "reset_at": "2100-01-01T00:00:00.443553353Z"},
        "limits": [
            {"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}, "detail": {"remaining": 20, "limit": 80}},
            {"name": "Opus", "detail": {"used": 5, "limit": 5, "reset_in": 60}},
            {"detail": {"nothing": True}},
        ],
    }
    status = parse_kimi_usage(payload)
    assert status.status == "exhausted"  # the Opus limit is fully used
    assert [(w.name, w.remaining_percent) for w in status.windows] == [("weekly", 70.0), ("5h", 25.0), ("Opus", 0.0)]
    assert status.windows[0].resets_at == "2100-01-01T00:00:00.443553+00:00"
    assert status.windows[2].resets_in_sec is not None and 55 <= status.windows[2].resets_in_sec <= 60


def test_parse_kimi_usage_empty_is_unknown():
    assert parse_kimi_usage({}).status == "unknown"
    assert parse_kimi_usage(None).status == "unknown"


@pytest.mark.asyncio
async def test_kimi_provider_explains_api_key_sessions(tmp_path: Path):
    status = await fetch_kimi_quota(
        AgentConfig(name="kimi", protocol="acp", command=["kimi", "acp"]),
        {"KIMI_CODE_HOME": str(tmp_path), "MOONSHOT_API_KEY": "sk-x"},
    )
    assert status.status == "unknown"
    assert "Open Platform" in (status.detail or "")


@pytest.mark.asyncio
async def test_kimi_provider_sends_bearer_token(tmp_path: Path, monkeypatch):
    creds = tmp_path / "credentials"
    creds.mkdir()
    (creds / "kimi-code.json").write_text(json.dumps({"access_token": "tok"}), encoding="utf-8")
    seen = {}

    async def fake_get(url, *, headers=None, env=None, timeout=10.0):
        seen["url"] = url
        seen["headers"] = headers
        return {"usage": {"used": 1, "limit": 4}}

    monkeypatch.setattr("agent_bridge.quota_kimi.get_json", fake_get)
    status = await fetch_kimi_quota(
        AgentConfig(name="kimi", protocol="acp", command=["kimi", "acp"]),
        {"KIMI_CODE_HOME": str(tmp_path)},
    )
    assert seen["url"].endswith("/usages")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert status.status == "ok" and status.windows[0].remaining_percent == 75.0


# --- unsupported DSH quota -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("experimental", [False, True])
async def test_dsh_quota_is_unknown_even_with_deepseek_key(monkeypatch, experimental):
    async def forbidden(*args, **kwargs):
        pytest.fail("DSH quota must not make an HTTP request")

    monkeypatch.setattr("agent_bridge.quota.get_json", forbidden)
    config = AppConfig(quota=QuotaConfig(experimental=experimental))
    row = await fetch_quota(
        _agent("dsh", "acp"), {"DEEPSEEK_API_KEY": "test-key"},
        cache=QuotaCache(60), timeout_sec=1, providers=provider_table(config),
    )
    assert row["status"] == "unknown"
    assert row["balance"] is None
    assert row["windows"] == []
    assert "not supported for dsh" in row["detail"]


# --- grok ----------------------------------------------------------------------


def test_grok_auth_headers_select_exact_scope_and_check_expiry(tmp_path: Path):
    from agent_bridge.quota_endpoints import GROK_AUTH_SCOPE

    headers, problem = grok_auth_headers(tmp_path, version="1.0.13")
    assert headers is None and "grok login" in problem
    entry = {"key": "official", "auth_mode": "oidc", "oidc_issuer": "https://auth.x.ai",
             "user_id": "test-user", "expires_at": "2100-01-01T00:00:00Z"}
    (tmp_path / "auth.json").write_text(json.dumps({
        "https://enterprise.example::client": {"key": "enterprise"}, GROK_AUTH_SCOPE: entry,
    }), encoding="utf-8")
    headers, problem = grok_auth_headers(tmp_path, version="1.0.13")
    assert problem is None
    assert headers == {
        "Authorization": "Bearer official", "X-XAI-Token-Auth": "xai-grok-cli", "x-userid": "test-user",
        "x-grok-client-version": "1.0.13", "x-grok-client-mode": "headless",
    }
    entry["expires_at"] = "1970-01-01T00:00:01Z"
    (tmp_path / "auth.json").write_text(json.dumps({GROK_AUTH_SCOPE: entry}), encoding="utf-8")
    headers, problem = grok_auth_headers(tmp_path, version="1.0.13", now=2000.0)
    assert headers is None and "expired" in problem


def test_parse_grok_billing_weekly_window_and_products():
    payload = {
        "config": {
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "x", "end": "2100-01-01T00:00:00Z"},
            "creditUsagePercent": 46.0,
            "productUsage": [{"product": "GrokBuild", "usagePercent": 41.0}],
        }
    }
    status = parse_grok_billing(payload)
    assert status.status == "ok"
    assert status.windows[0].name == "weekly"
    assert status.windows[0].remaining_percent == 54.0
    assert status.windows[0].resets_at == "2100-01-01T00:00:00+00:00"
    assert "GrokBuild 41% used" in (status.detail or "")
    assert parse_grok_billing({"config": {}}).status == "unknown"
    assert parse_grok_billing({"config": {"creditUsagePercent": 100}}).status == "exhausted"


@pytest.mark.asyncio
async def test_grok_provider_explains_api_key_sessions(tmp_path: Path):
    status = await fetch_grok_quota(
        AgentConfig(name="grok", protocol="acp", command=["grok"]),
        {"GROK_HOME": str(tmp_path), "XAI_API_KEY": "xai-x"},
    )
    assert status.status == "unknown"
    assert "API-key" in (status.detail or "")


# --- claude --------------------------------------------------------------------


def test_claude_access_token_reads_oauth_block(tmp_path: Path):
    token, problem = claude_access_token(tmp_path)
    assert token is None and "claude auth login" in problem
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": FAR_FUTURE * 1000}}), encoding="utf-8"
    )
    assert claude_access_token(tmp_path) == ("tok", None)
    # Claude stores expiresAt in epoch milliseconds.
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": 1_700_000_000_000}}), encoding="utf-8"
    )
    token, problem = claude_access_token(tmp_path, now=1_800_000_000.0)
    assert token is None and "expired" in problem


def test_parse_claude_usage_windows():
    payload = {
        "five_hour": {"utilization": 74.0, "resets_at": "2100-01-01T00:00:00Z"},
        "seven_day": {"utilization": 10.0, "resets_at": FAR_FUTURE},
        "seven_day_opus": {"utilization": 100.0, "resets_at": None},
    }
    status = parse_claude_usage(payload)
    assert status.status == "ok"
    assert "requested model" in status.detail
    assert [(w.name, w.remaining_percent) for w in status.windows] == [
        ("5h", 26.0),
        ("weekly", 90.0),
        ("weekly:opus", 0.0),
    ]
    assert status.windows[0].resets_at == status.windows[1].resets_at == "2100-01-01T00:00:00+00:00"
    assert parse_claude_usage({}).status == "unknown"


@pytest.mark.parametrize("depleted", ["seven_day_opus", "seven_day_sonnet"])
def test_claude_model_limit_does_not_exhaust_shared_quota(depleted):
    payload = {
        "five_hour": {"utilization": 20},
        "seven_day": {"utilization": 30},
        "seven_day_opus": {"utilization": 10},
        "seven_day_sonnet": {"utilization": 10},
    }
    payload[depleted]["utilization"] = 100
    status = parse_claude_usage(payload)
    assert status.status == "ok"
    assert [window.remaining_percent for window in status.windows[:2]] == [80.0, 70.0]
    assert sorted(window.remaining_percent for window in status.windows[2:]) == [0.0, 90.0]


@pytest.mark.parametrize("depleted", ["five_hour", "seven_day"])
def test_claude_shared_limit_still_exhausts_quota(depleted):
    payload = {
        "five_hour": {"utilization": 20},
        "seven_day": {"utilization": 30},
        "seven_day_opus": {"utilization": 10},
        "seven_day_sonnet": {"utilization": 10},
    }
    payload[depleted]["utilization"] = 100
    status = parse_claude_usage(payload)
    assert status.status == "exhausted"
    assert [window.remaining_percent for window in status.windows[2:]] == [90.0, 90.0]


@pytest.mark.parametrize("utilization", [0, 100, None])
def test_claude_model_windows_cannot_establish_shared_quota(utilization):
    status = parse_claude_usage({
        "five_hour": {"resets_at": FAR_FUTURE},
        "seven_day_opus": {"utilization": utilization, "resets_at": FAR_FUTURE},
    })
    assert status.status == "unknown"
    assert len(status.windows) == 2
    assert status.windows[1].name == "weekly:opus"
    assert status.windows[1].remaining_percent == (None if utilization is None else 100 - utilization)
    assert status.windows[1].resets_at == "2100-01-01T00:00:00+00:00"
    assert "Shared Claude quota is unknown" in status.detail


@pytest.mark.asyncio
async def test_claude_provider_only_for_oauth_logins(tmp_path: Path):
    status = await fetch_claude_quota(
        AgentConfig(name="claude", protocol="acp", command=["claude-agent-acp"]),
        {"CLAUDE_CONFIG_DIR": str(tmp_path), "ANTHROPIC_API_KEY": "sk-ant"},
    )
    assert status.status == "unknown"
    assert "auth=api-key" in (status.detail or "")


@pytest.mark.asyncio
async def test_claude_provider_sends_oauth_headers(tmp_path: Path, monkeypatch):
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8"
    )
    seen = {}

    async def fake_get(url, *, headers=None, env=None, timeout=10.0):
        seen["url"] = url
        seen["headers"] = headers
        return {
            "five_hour": {"utilization": 50, "resets_at": FAR_FUTURE},
            "seven_day_opus": {"utilization": 100, "resets_at": FAR_FUTURE},
        }

    monkeypatch.setattr("agent_bridge.quota_claude.get_json", fake_get)
    status = await fetch_claude_quota(
        AgentConfig(name="claude", protocol="acp", command=["claude-agent-acp"]),
        {"CLAUDE_CONFIG_DIR": str(tmp_path)},
    )
    assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert seen["headers"]["User-Agent"].startswith("claude-code/")
    assert status.status == "ok" and status.windows[0].remaining_percent == 50.0
    assert status.windows[1].name == "weekly:opus" and status.windows[1].remaining_percent == 0.0
    assert "requested model" in status.detail


# --- config --------------------------------------------------------------------


def test_quota_config_defaults_and_overlay(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.quota == QuotaConfig(enabled=True, timeout_sec=4.0, cache_sec=300.0, experimental=False)
    assert cfg.warnings == []
    (tmp_path / "agents.toml").write_text(
        "[quota]\nenabled = false\ntimeout_sec = 1.5\ncache_sec = 0\nexperimental = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.quota == QuotaConfig(enabled=False, timeout_sec=1.5, cache_sec=0.0, experimental=True)
    assert cfg.warnings == []


# --- describe_quotas (CLI payload) --------------------------------------------


@pytest.mark.asyncio
async def test_describe_quotas_covers_every_agent(monkeypatch):
    config = AppConfig(
        agents={
            "fake": AgentConfig(name="fake", protocol="fake", command=["fake"]),
            "ghost": AgentConfig(name="ghost", protocol="acp", command=["definitely-not-installed-xyz"]),
        }
    )
    monkeypatch.setattr("agent_bridge.quota.QuotaCache", QuotaCache)
    rows = await describe_quotas(config)
    assert set(rows) == {"fake", "ghost"}
    assert rows["fake"]["status"] == "unknown" and "not supported" in rows["fake"]["detail"]
    assert rows["ghost"]["status"] == "unknown" and "not found" in rows["ghost"]["detail"]


def test_far_future_fixture_is_actually_in_the_future():
    assert datetime.fromtimestamp(FAR_FUTURE, tz=UTC) > datetime.now(UTC) + timedelta(days=365)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_cached_countdown_advances_without_mutating_reading(monkeypatch, stale):
    now = datetime(2099, 12, 31, 23, 59, tzinfo=UTC)
    original = QuotaStatus(status="ok", windows=[window_from_reset("5h", 50, FAR_FUTURE, now=now)])
    cache = QuotaCache(300)
    cache.put("fake", original)
    if stale:
        cache.invalidate("fake")

    async def broken(cfg, env):
        raise RuntimeError("offline")

    monkeypatch.setattr("agent_bridge.quota.utcnow", lambda: now + timedelta(seconds=20))
    row = await fetch_quota(_agent(), {}, cache=cache, timeout_sec=1, providers={"fake": QuotaProvider(None, broken)})
    assert row["windows"][0]["resets_in_sec"] == 40
    assert row["stale"] is stale
    monkeypatch.setattr("agent_bridge.quota.utcnow", lambda: now + timedelta(seconds=70))
    row = await fetch_quota(_agent(), {}, cache=cache, timeout_sec=1, providers={"fake": QuotaProvider(None, broken)})
    assert row["status"] == "unknown"
    assert row["windows"] == []
    assert original.windows[0].resets_in_sec == 60


@pytest.mark.parametrize("failure", [False, True])
async def test_cached_quota_refetches_after_window_reset(monkeypatch, failure):
    now = datetime(2099, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("agent_bridge.quota.utcnow", lambda: now)
    cache = QuotaCache(300)
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        if calls == 1:
            return summarize_quota([
                window_from_reset("weekly", 70, (now + timedelta(days=1)).isoformat()),
                window_from_reset("5h", 0, (now + timedelta(seconds=1)).isoformat()),
            ])
        if failure:
            raise RuntimeError("refresh failed")
        return summarize_quota([window_from_reset("5h", 100, (now + timedelta(hours=5)).isoformat())])

    table = {"fake": QuotaProvider("test", provider)}
    try:
        assert (await fetch_quota(_agent(), {}, cache=cache, timeout_sec=1, providers=table))["status"] == "exhausted"
        assert (await fetch_quota(_agent(), {}, cache=cache, timeout_sec=1, providers=table))["cached"]
        assert calls == 1
        now += timedelta(seconds=1.2)
        row = await fetch_quota(_agent(), {}, cache=cache, timeout_sec=1, providers=table)
        assert calls == 2
        assert row["status"] == ("unknown" if failure else "ok")
        assert not row["stale"]
        assert not row["cached"]
        if failure:
            assert row["windows"] == []
            assert cache.last_good("fake") is None
    finally:
        await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_concurrent_fetches_share_read_and_isolate_cancellation(failure):
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        if failure:
            raise RuntimeError("offline")
        return QuotaStatus(status="ok")

    cache = QuotaCache(300)

    async def fetch():
        return await fetch_quota(_agent(), {}, cache=cache, timeout_sec=1, providers={"fake": QuotaProvider(None, provider)})

    first = asyncio.create_task(fetch())
    await started.wait()
    second = asyncio.create_task(fetch())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await second
    assert result["status"] == ("unknown" if failure else "ok")
    assert calls == 1
    assert (await fetch())["cached"] is True
    assert calls == 1
    await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", [False, True])
async def test_invalidation_during_read_cannot_restore_old_cache(clear):
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        number = calls
        if number == 1:
            started.set()
            await release.wait()
        return QuotaStatus(status="ok", plan=str(number))

    cache = QuotaCache(300)

    async def fetch():
        return await fetch_quota(_agent(), {}, cache=cache, timeout_sec=1, providers={"fake": QuotaProvider(None, provider)})

    old = asyncio.create_task(fetch())
    await started.wait()
    if clear:
        cache.clear()
    else:
        cache.invalidate("fake")
    assert (await fetch())["plan"] == "2"
    release.set()
    assert (await old)["plan"] == "1"
    assert (await fetch())["plan"] == "2"
    assert cache.last_good("fake").plan == "2"
    await cache.close()


@pytest.mark.asyncio
async def test_disabled_cli_does_not_prepare_or_query_workers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled quota must not prepare a lookup")

    monkeypatch.setattr("agent_bridge.quota.provider_table", forbidden)
    monkeypatch.setattr("agent_bridge.probes.command_exists", forbidden)
    monkeypatch.setattr("agent_bridge.worker_env.build_worker_env", forbidden)
    config = AppConfig(agents={"fake": _agent(), "ghost": _agent("ghost", "acp")}, quota=QuotaConfig(enabled=False))
    rows = await describe_quotas(config)
    assert set(rows) == {"fake", "ghost"}
    for row in rows.values():
        assert row["status"] == "unknown"
        assert "disabled" in row["detail"]


@pytest.mark.asyncio
async def test_cli_command_check_uses_global_codex_environment(monkeypatch):
    from agent_bridge.config import EnvConfig

    def resolve(command, fallbacks=None, *, env=None):
        if env.get("CODEX_CLI_PATH") != "configured-codex":
            raise FileNotFoundError("missing configured Codex")
        return [env["CODEX_CLI_PATH"]]

    async def provider(cfg, env):
        assert env["CODEX_CLI_PATH"] == "configured-codex"
        return QuotaStatus(status="ok")

    monkeypatch.setattr("agent_bridge.probes.resolve_codex_command", resolve)
    monkeypatch.setattr("agent_bridge.quota.provider_table", lambda config: {"protocol:codex": QuotaProvider(None, provider)})
    config = AppConfig(
        agents={"codex-alt": _agent("codex-alt", "codex")},
        env=EnvConfig(discover_proxy=False, inherit=[], set={"CODEX_CLI_PATH": "configured-codex"}),
    )
    assert (await describe_quotas(config))["codex-alt"]["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True])
async def test_codex_timeout_returns_before_resistant_child_cleanup(monkeypatch, answered):
    import os

    from agent_bridge import processes

    # A real subprocess; suppress the first termination signal to model a
    # resistant child portably, including Windows where SIGTERM is a hard kill.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    real_kill = processes.kill_tree
    signals = []

    def resist(pid, handle=None, *, force=False):
        if pid == proc.pid:
            signals.append(force)
            if not force:
                return
        real_kill(pid, handle=handle, force=force)

    async def spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(processes, "kill_tree", resist)
    monkeypatch.setattr("agent_bridge.quota_codex.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("agent_bridge.quota_codex.resolve_codex_command", lambda *args, **kwargs: [sys.executable])
    if answered:

        async def response(*args):
            return {"rateLimits": {}}

        monkeypatch.setattr("agent_bridge.quota_codex._read_response", response)
        monkeypatch.setattr("agent_bridge.quota_codex.POST_INIT_SETTLE_SEC", 0)
    cache = QuotaCache(60)
    try:
        start = time.monotonic()
        row = await fetch_quota(
            _agent("codex", "codex"),
            dict(os.environ),
            cache=cache,
            timeout_sec=0.1,
            providers={"protocol:codex": QuotaProvider(None, fetch_codex_quota)},
        )
        assert time.monotonic() - start < 0.6
        assert row["status"] == "unknown" and "timed out" in row["detail"]
        # Closing must not cancel the cancellation cleanup a second time.
        await asyncio.wait_for(cache.close(), timeout=6)
        assert proc.returncode is not None
        assert signals == [False, True]
    finally:
        real_kill(proc.pid, handle=proc, force=True)
        await proc.wait()
