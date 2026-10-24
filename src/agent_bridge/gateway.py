"""Where a worker's model requests go, read from its pinned configuration.

``list_agents`` shows this so a coordinator sees that a worker runs through a
local gateway and which model ids that gateway offers. Only what ``agents.toml``
pins is reported: a worker's own config files and the process environment are
not read, and nothing is probed over the network.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_bridge.config import AgentConfig
from agent_bridge.worker_env import redact_proxy_url

# A gateway can offer dozens of models; the coordinator needs a usable sample,
# not the catalog.
ROUTE_MODELS_MAX = 20
ANTHROPIC_DIRECT = "https://api.anthropic.com"


def _url(value: Any) -> str | None:
    text = str(value or "").strip()
    return redact_proxy_url(text) if text else None


def _opencode_route(config_path: str) -> dict[str, Any]:
    try:
        raw = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"problem": f"OPENCODE_CONFIG is unreadable: {config_path}"}
    providers = raw.get("provider") if isinstance(raw, dict) else None
    if not isinstance(providers, Mapping):
        return {"problem": "OPENCODE_CONFIG defines no provider"}
    enabled = raw.get("enabled_providers")
    names = [name for name in providers if not isinstance(enabled, list) or name in enabled]
    models = [
        f"{name}/{model}"
        for name in names
        if isinstance(providers[name], Mapping) and isinstance(providers[name].get("models"), Mapping)
        for model in providers[name]["models"]
    ]
    first = providers.get(names[0]) if names else None
    options = first.get("options") if isinstance(first, Mapping) else None
    return {
        "via": names[0] if len(names) == 1 else None,
        "base_url": _url(options.get("baseURL")) if isinstance(options, Mapping) else None,
        "models": models[:ROUTE_MODELS_MAX],
        "models_more": max(0, len(models) - ROUTE_MODELS_MAX),
    }


def _codex_provider(overrides: Sequence[str]) -> Mapping[str, Any] | None:
    """The custom model provider a codex ``-c`` override list selects, if any.

    Each item is one ``key=value`` TOML line, so dotted keys such as
    ``model_providers.magpie.base_url`` parse into nested tables; items that are
    not valid TOML are ignored (codex rejects them at launch, not here).
    """
    provider_id: Any = None
    tables: dict[str, dict[str, Any]] = {}
    for item in overrides:
        try:
            parsed = tomllib.loads(str(item))
        except tomllib.TOMLDecodeError:
            continue
        provider_id = parsed.get("model_provider", provider_id)
        for name, table in (parsed.get("model_providers") or {}).items():
            if isinstance(table, dict):
                tables.setdefault(name, {}).update(table)
    if not isinstance(provider_id, str) or provider_id not in tables:
        return None
    return {"id": provider_id, **tables[provider_id]}


def describe_route(cfg: AgentConfig) -> dict[str, Any] | None:
    """The gateway ``cfg`` pins for a worker, or None when it talks to its vendor."""
    provider = _codex_provider(cfg.config_overrides) if cfg.protocol == "codex" else None
    if provider:
        route: dict[str, Any] | None = {
            "via": re.sub(r"\s+", " ", str(provider.get("name") or provider["id"])).strip(),
            "base_url": _url(provider.get("base_url")),
        }
    elif cfg.name == "opencode" and cfg.env.get("OPENCODE_CONFIG"):
        route = _opencode_route(cfg.env["OPENCODE_CONFIG"])
    elif cfg.name == "claude" and (cfg.env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/") not in {
        "",
        ANTHROPIC_DIRECT,
    }:
        route = {"base_url": _url(cfg.env["ANTHROPIC_BASE_URL"])}
    else:
        return None
    return {key: value for key, value in route.items() if value not in (None, "", [], 0)}
