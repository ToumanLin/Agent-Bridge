"""list_agents shows the gateway a worker is pinned to, from agents.toml alone."""

import json

from agent_bridge.config import AgentConfig
from agent_bridge.gateway import ROUTE_MODELS_MAX, describe_route
from agent_bridge.public import agent_view, agents_view


def _agent(name: str, protocol: str = "acp", **extra) -> AgentConfig:
    return AgentConfig(name=name, protocol=protocol, command=[name], **extra)


def _opencode_file(tmp_path, models, **top):
    path = tmp_path / "oc.json"
    body = {"provider": {"magpie": {"options": {"baseURL": "http://127.0.0.1:3425/v1"}, "models": models}}}
    path.write_text(json.dumps({**body, **top}), encoding="utf-8")
    return str(path)


def test_worker_without_a_pinned_gateway_has_no_route():
    assert describe_route(_agent("grok")) is None
    assert describe_route(_agent("codex", "codex")) is None
    assert describe_route(_agent("claude")) is None
    assert describe_route(_agent("claude", env={"ANTHROPIC_BASE_URL": "https://api.anthropic.com/"})) is None


def test_codex_route_names_the_provider_and_drops_credentials_from_the_url():
    cfg = _agent(
        "codex",
        "codex",
        config_overrides=[
            'model_provider="magpie"',
            'model_providers.magpie.name="magpie"',
            'model_providers.magpie.base_url="http://user:pw@127.0.0.1:3425/v1"',
        ],
    )
    assert describe_route(cfg) == {"via": "magpie", "base_url": "http://***@127.0.0.1:3425"}


def test_codex_overrides_without_a_custom_provider_have_no_route():
    cfg = _agent("codex", "codex", config_overrides=['model_reasoning_effort="high"', "not toml"])
    assert describe_route(cfg) is None
    cfg = _agent("codex", "codex", config_overrides=['model_provider="missing"'])
    assert describe_route(cfg) is None


def test_claude_route_is_its_base_url():
    cfg = _agent("claude", env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:3425"})
    assert describe_route(cfg) == {"base_url": "http://127.0.0.1:3425"}


def test_opencode_route_lists_provider_prefixed_models(tmp_path):
    path = _opencode_file(tmp_path, {"claude/a": {}, "gpt/b": {}})
    route = describe_route(_agent("opencode", env={"OPENCODE_CONFIG": path}))
    assert route == {
        "via": "magpie",
        "base_url": "http://127.0.0.1:3425",
        "models": ["magpie/claude/a", "magpie/gpt/b"],
    }


def test_opencode_route_caps_the_model_list(tmp_path):
    path = _opencode_file(tmp_path, {f"m{i}": {} for i in range(ROUTE_MODELS_MAX + 5)})
    route = describe_route(_agent("opencode", env={"OPENCODE_CONFIG": path}))
    assert len(route["models"]) == ROUTE_MODELS_MAX
    assert route["models_more"] == 5


def test_opencode_route_honours_enabled_providers(tmp_path):
    path = _opencode_file(tmp_path, {"a": {}}, enabled_providers=["other"])
    assert describe_route(_agent("opencode", env={"OPENCODE_CONFIG": path})) == {}


def test_opencode_route_reports_an_unreadable_config(tmp_path):
    route = describe_route(_agent("opencode", env={"OPENCODE_CONFIG": str(tmp_path / "missing.json")}))
    assert "unreadable" in route["problem"]


def test_agent_view_carries_the_route_only_when_there_is_one():
    row = {"agent": "grok", "available": True}
    assert "route" not in agent_view(row)
    routed = {**row, "route": {"via": "magpie"}}
    assert agent_view(routed)["route"] == {"via": "magpie"}
    assert agents_view([routed], {}, {})["agents"][0]["route"] == {"via": "magpie"}
