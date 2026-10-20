from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from agent_bridge.paths import bridge_home, ensure_home
from agent_bridge.processes import count_sibling_servers
from agent_bridge.worker_env import is_worker_context

log = logging.getLogger(__name__)

GIT_SOURCE = "git+https://github.com/FeiZhuLulu/Agent-Bridge.git"
PACKAGE = "agent-bridge"

# Where releases before the skill was retired installed the coordinator
# skill. Nothing writes these any more; retire_coordinator_skill removes
# the copies those releases left behind.
SKILL_RELATIVE = (
    Path(".agents") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".cursor") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".codex") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".kimi-code") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".zcode") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".claude") / "skills" / "agent-bridge" / "SKILL.md",
)

HELP = f"""Agent Bridge — connect local coding agents.

Usage:
  agent-bridge                 start the MCP server on stdio
  agent-bridge --env           print reconstructed worker environment and other running instances
  agent-bridge --agents        print each worker's probe: version, or why it is unavailable
  agent-bridge --quota        print each worker's remaining quota (fresh, no cache)
  agent-bridge --version       print the installed version
  agent-bridge upgrade         one-command update (close coordinators first)
  agent-bridge help            this text

Install:
  uv tool install {GIT_SOURCE}

Then register the MCP server in Codex, Cursor, Kimi Code, ZCode, Grok
Build, or Claude Code (Devin reads the Cursor entry). The MCP handshake
carries the coordinator rules; no skill file is installed.

Update:
  agent-bridge upgrade
"""


def package_version() -> str:
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return "0+unknown"


def skill_destinations(home: Path) -> list[Path]:
    return [home / rel for rel in SKILL_RELATIVE]


def detect_install(
    *,
    executable: Path | None = None,
    start: Path | None = None,
) -> str:
    """Return 'uv-tool', 'checkout', or 'unknown'."""
    exe = Path(executable or sys.executable).resolve()
    parts = [part.lower() for part in exe.parts]
    try:
        index = parts.index("tools")
    except ValueError:
        index = -1
    if index >= 0 and "uv" in parts[:index] and PACKAGE in parts[index:]:
        return "uv-tool"

    here = Path(start or __file__).resolve()
    for parent in here.parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file() or not (parent / ".git").exists():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if f'name = "{PACKAGE}"' in text:
            return "checkout"
    return "unknown"


def checkout_root(start: Path | None = None) -> Path | None:
    here = Path(start or __file__).resolve()
    for parent in here.parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.is_file() and (parent / ".git").exists():
            try:
                if f'name = "{PACKAGE}"' in pyproject.read_text(encoding="utf-8"):
                    return parent
            except OSError:
                continue
    return None


def skill_stamp_path(home: Path | None = None) -> Path:
    root = bridge_home() if home is None else home / ".agent-bridge"
    return root / "skill.sha256"


def _is_bridge_skill(path: Path) -> bool:
    """True when ``path`` is a coordinator skill an older Bridge installed."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return False
    if not head.startswith("---"):
        return False
    front = head.split("---", 2)[1] if head.count("---") >= 2 else ""
    return any(line.strip() == "name: agent-bridge" for line in front.splitlines())


def retire_coordinator_skill(*, home: Path | None = None) -> list[Path]:
    """Delete the coordinator skill copies older releases installed.

    Only a ``SKILL.md`` whose front matter names ``agent-bridge`` is removed,
    then its folder if that leaves it empty, then the install stamp.
    Fail-open: a file that cannot be removed is logged and skipped.
    """
    removed: list[Path] = []
    for dest in skill_destinations(home or Path.home()):
        if not dest.is_file() or not _is_bridge_skill(dest):
            continue
        try:
            dest.unlink()
        except OSError:
            log.warning("could not remove old coordinator skill %s", dest, exc_info=True)
            continue
        removed.append(dest)
        # Not empty: the user keeps other files there.
        with contextlib.suppress(OSError):
            dest.parent.rmdir()
    try:
        skill_stamp_path(home).unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove the skill install stamp", exc_info=True)
    return removed


def retire_skill_on_start(*, home: Path | None = None) -> list[Path]:
    """Run the cleanup once at a top-level start, while an install stamp remains.

    The first ``upgrade`` from an older release runs that release's code, which
    does not clean up, so the new server finishes the job on its first start.
    Nested worker-inherited Bridges and pytest runs never touch host files.
    """
    if is_worker_context():
        return []
    if home is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    if not skill_stamp_path(home).exists():
        return []
    return retire_coordinator_skill(home=home)


def upgrade(
    *,
    kind: str | None = None,
    siblings: int | None = None,
    run: Callable[..., Any] | None = None,
) -> list[str]:
    live = count_sibling_servers() if siblings is None else siblings
    if live:
        raise RuntimeError(
            f"{live} Agent Bridge instance(s) are still running; close every "
            "coordinator that is holding a Bridge connection, then retry"
        )
    detected = kind or detect_install()
    runner = run or subprocess.run
    notes: list[str] = []
    if detected == "uv-tool":
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("uv is not on PATH; install uv, then retry")
        completed = runner([uv, "tool", "upgrade", PACKAGE], check=False)
        if getattr(completed, "returncode", 0) not in (0, None):
            raise RuntimeError("uv tool upgrade agent-bridge failed")
        notes.append("uv tool upgrade agent-bridge")
    elif detected == "checkout":
        root = checkout_root()
        if root is None:
            raise RuntimeError("could not find the Agent Bridge git checkout")
        git = shutil.which("git")
        uv = shutil.which("uv")
        if not git or not uv:
            raise RuntimeError("git and uv must be on PATH to upgrade a checkout")
        for command in ([git, "pull"], [uv, "sync", "--extra", "dev"]):
            completed = runner(command, cwd=str(root), check=False)
            if getattr(completed, "returncode", 0) not in (0, None):
                raise RuntimeError(f"{' '.join(command)} failed in {root}")
        notes.append(f"git pull && uv sync --extra dev in {root}")
    else:
        raise RuntimeError(
            f"this copy is neither a uv tool install nor a git checkout. Install with: uv tool install {GIT_SOURCE}"
        )
    removed = retire_coordinator_skill()
    if removed:
        notes.append("removed old coordinator skill " + ", ".join(str(path) for path in removed))
    return notes


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        from agent_bridge.logging_setup import setup_logging
        from agent_bridge.server import mcp

        setup_logging(ensure_home())
        retire_skill_on_start()
        mcp.run(transport="stdio")
        return

    command = args[0]
    if command in {"-h", "--help", "help"}:
        print(HELP, end="")
        return
    if command in {"--version", "-V", "version"}:
        print(package_version())
        return
    if command in {"--env", "env", "--print-env"}:
        from agent_bridge.config import load_config
        from agent_bridge.worker_env import describe_env

        # One instance per open coordinator host is normal; a pile-up means a
        # host keeps abandoning spawns (they self-exit after server.idle_exit_sec).
        status = {**describe_env(load_config().env), "other_instances": count_sibling_servers()}
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return
    if command in {"--agents", "agents"}:
        import asyncio

        from agent_bridge.config import load_config
        from agent_bridge.probes import probe_agent

        # The MCP list_agents only names a missing worker; the reason and any
        # install hint are for the person reading this.
        config = load_config()

        async def probe_all() -> list[dict]:
            return list(await asyncio.gather(*(probe_agent(cfg, config.env) for cfg in config.agents.values())))

        print(json.dumps(asyncio.run(probe_all()), indent=2, ensure_ascii=False))
        return
    if command in {"--quota", "quota"}:
        import asyncio

        from agent_bridge.config import load_config
        from agent_bridge.quota import describe_quotas

        print(json.dumps(asyncio.run(describe_quotas(load_config())), indent=2, ensure_ascii=False))
        return
    if command in {"upgrade", "--upgrade"}:
        for note in upgrade():
            print(note)
        print("restart each coordinator so it reconnects")
        return
    raise SystemExit(f"unknown command: {command}\n{HELP}")
