from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_bridge.cli import (
    checkout_root,
    detect_install,
    main,
    retire_coordinator_skill,
    retire_skill_on_start,
    skill_destinations,
    skill_stamp_path,
    upgrade,
)

OLD_SKILL = "---\nname: agent-bridge\ndescription: old coordinator skill\n---\n\n# Dispatching workers\n"


def _plant_old_skill(home, *, stamp: bool = True):
    for dest in skill_destinations(home):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(OLD_SKILL, encoding="utf-8")
    if stamp:
        path = skill_stamp_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("abc\n", encoding="utf-8")


def test_detect_uv_tool_install(tmp_path):
    exe = tmp_path / "uv" / "tools" / "agent-bridge" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    assert detect_install(executable=exe, start=tmp_path / "nope.py") == "uv-tool"


def test_detect_checkout():
    assert detect_install() == "checkout"
    assert checkout_root() is not None
    assert (checkout_root() / "pyproject.toml").is_file()


def test_retire_removes_old_copies_and_stamp(tmp_path):
    _plant_old_skill(tmp_path)
    removed = retire_coordinator_skill(home=tmp_path)
    assert removed == skill_destinations(tmp_path)
    for dest in removed:
        assert not dest.exists()
        assert not dest.parent.exists()
    assert not skill_stamp_path(tmp_path).exists()


def test_retire_keeps_foreign_skill_and_other_files(tmp_path):
    dests = skill_destinations(tmp_path)
    foreign = dests[0]
    foreign.parent.mkdir(parents=True)
    foreign.write_text("---\nname: my-own-skill\n---\n", encoding="utf-8")
    ours = dests[1]
    ours.parent.mkdir(parents=True)
    ours.write_text(OLD_SKILL, encoding="utf-8")
    (ours.parent / "notes.md").write_text("mine", encoding="utf-8")
    assert retire_coordinator_skill(home=tmp_path) == [ours]
    assert foreign.is_file()
    assert not ours.exists()
    assert (ours.parent / "notes.md").is_file()


def test_retire_on_start_needs_install_stamp(tmp_path):
    _plant_old_skill(tmp_path, stamp=False)
    assert retire_skill_on_start(home=tmp_path) == []
    assert all(dest.is_file() for dest in skill_destinations(tmp_path))
    skill_stamp_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    skill_stamp_path(tmp_path).write_text("abc\n", encoding="utf-8")
    assert len(retire_skill_on_start(home=tmp_path)) == 6
    assert retire_skill_on_start(home=tmp_path) == []


def test_retire_on_start_skips_worker_context_and_pytest(tmp_path, monkeypatch):
    _plant_old_skill(tmp_path)
    monkeypatch.setattr("agent_bridge.cli.Path.home", lambda: tmp_path)
    assert retire_skill_on_start() == []  # PYTEST_CURRENT_TEST without an explicit home
    monkeypatch.setenv("AGENT_BRIDGE_PARENT_CONTEXT", "worker")
    assert retire_skill_on_start(home=tmp_path) == []
    assert all(dest.is_file() for dest in skill_destinations(tmp_path))


def test_upgrade_refuses_while_siblings_live():
    with pytest.raises(RuntimeError, match="still running"):
        upgrade(siblings=2, kind="uv-tool")


def test_upgrade_uv_tool(monkeypatch, tmp_path):
    _plant_old_skill(tmp_path)
    monkeypatch.setattr("agent_bridge.cli.Path.home", lambda: tmp_path)
    monkeypatch.setattr("agent_bridge.cli.shutil.which", lambda name: f"/bin/{name}")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    notes = upgrade(kind="uv-tool", siblings=0, run=fake_run)
    assert calls == [["/bin/uv", "tool", "upgrade", "agent-bridge"]]
    assert not any(dest.exists() for dest in skill_destinations(tmp_path))
    assert any("removed old coordinator skill" in note for note in notes)
    assert any("uv tool upgrade" in note for note in notes)


def test_upgrade_unknown_tells_user_to_install():
    with pytest.raises(RuntimeError, match="uv tool install"):
        upgrade(kind="unknown", siblings=0)


def test_help_and_version(capsys):
    main(["help"])
    out = capsys.readouterr().out
    assert "uv tool install" in out
    assert "Claude Code" in out
    assert "install-skill" not in out
    main(["--version"])
    assert capsys.readouterr().out.strip()
