import json
import subprocess
import sys
from pathlib import Path

from server import setup as _setup_impl
from server.setup import SetupError, install_setup, restore_setup, setup_plan


def test_setup_plan_describes_user_scope_and_managed_launchers(tmp_path):
    plan = setup_plan(
        Path("/package"),
        tmp_path / "home",
        ["codex", "claude"],
        executable_paths={
            "codex": tmp_path / "home" / ".local/bin/codex",
            "claude": tmp_path / "home" / ".local/bin/claude",
        },
    )

    assert plan["bundle"] == str(tmp_path / "home" / ".local/share/session-handoff/plugin")
    assert plan["skills"]["codex"] == str(tmp_path / "home" / ".codex/skills/session-handoff/SKILL.md")
    assert plan["skills"]["claude"] == str(tmp_path / "home" / ".claude/skills/session-handoff/SKILL.md")
    assert plan["launchers"] == {
        "codex": str(tmp_path / "home" / ".local/bin/codex"),
        "claude": str(tmp_path / "home" / ".local/bin/claude"),
    }


def test_setup_installs_skill_mcp_registration_and_launcher(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    for client in ("codex", "claude"):
        original = bin_dir / client
        original.write_text(f'#!/bin/sh\nexec {client} "$@"\n', encoding="utf-8")
        original.chmod(0o755)

    calls = []

    def runner(argv):
        calls.append(argv)

    result = install_setup(
        package,
        home,
        ["codex", "claude"],
        executable_paths={client: bin_dir / client for client in ("codex", "claude")},
        runner=runner,
    )

    assert (home / ".codex/skills/session-handoff/SKILL.md").is_file()
    assert (home / ".claude/skills/session-handoff/SKILL.md").is_file()
    assert (home / ".local/share/session-handoff/plugin/server/handoff_mcp.py").is_file()
    assert not (home / ".local/share/session-handoff/plugin/.git").exists()
    assert (bin_dir / "codex.session-handoff-original").is_file()
    assert (bin_dir / "claude.session-handoff-original").is_file()
    assert "run codex" in (bin_dir / "codex").read_text(encoding="utf-8")
    assert "run claude" in (bin_dir / "claude").read_text(encoding="utf-8")
    assert any(call[1:5] == ["mcp", "add", "session-handoff", "--"] for call in calls if call[0].endswith("/codex"))
    assert any(call[1:6] == ["mcp", "add", "--scope", "user", "session-handoff"] for call in calls if call[0].endswith("/claude"))
    state = json.loads((home / ".config/session-handoff/state.json").read_text(encoding="utf-8"))
    assert state["clients"] == ["codex", "claude"]
    assert result["installed"] is True


def test_setup_is_idempotent_and_preserves_existing_skill(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    original = bin_dir / "codex"
    original.write_text("original", encoding="utf-8")
    calls = []

    install_setup(
        package,
        home,
        ["codex"],
        executable_paths={"codex": original},
        runner=calls.append,
    )
    first_skill = (home / ".codex/skills/session-handoff/SKILL.md").read_text(encoding="utf-8")

    result = install_setup(
        package,
        home,
        ["codex"],
        executable_paths={"codex": bin_dir / "codex"},
        runner=calls.append,
    )

    assert result["installed"] is True
    assert (home / ".codex/skills/session-handoff/SKILL.md").read_text(encoding="utf-8") == first_skill
    assert len(calls) == 1


def test_setup_refuses_to_overwrite_a_different_user_skill(tmp_path):
    package = Path(__file__).parents[1]
    skill = tmp_path / "home/.codex/skills/session-handoff/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("user-owned skill", encoding="utf-8")

    try:
        install_setup(
            package,
            tmp_path / "home",
            ["codex"],
            executable_paths={"codex": tmp_path / "home/.local/bin/codex"},
            runner=lambda _: None,
        )
    except SetupError as exc:
        assert "user-owned" in str(exc)
    else:
        raise AssertionError("setup must not overwrite an existing user skill")


def test_managed_launcher_forwards_host_help_flag(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    seen = tmp_path / "args.json"
    original = bin_dir / "codex"
    original.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(seen)!r}).write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    original.chmod(0o755)
    runner = lambda _: None

    install_setup(
        package,
        home,
        ["codex"],
        executable_paths={"codex": original},
        runner=runner,
    )

    result = subprocess.run([str(bin_dir / "codex"), "--help"], check=False)

    assert result.returncode == 0
    args = json.loads(seen.read_text(encoding="utf-8"))
    assert args[-1] == "--help"
    assert args[0] == "-c"
    assert "mcp_servers.session-handoff.env.SESSION_HANDOFF_CONTROL=" in args[1]


def test_setup_refreshes_the_persistent_bundle_on_reinstall(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    original = home / ".local/bin/codex"
    original.parent.mkdir(parents=True)
    original.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    original.chmod(0o755)

    install_setup(
        package,
        home,
        ["codex"],
        executable_paths={"codex": original},
        runner=lambda _: None,
    )
    bundle_entrypoint = home / ".local/share/session-handoff/plugin/bin/session-handoff"
    bundle_entrypoint.write_text("stale", encoding="utf-8")

    install_setup(
        package,
        home,
        ["codex"],
        executable_paths={"codex": home / ".local/bin/codex"},
        runner=lambda _: (_ for _ in ()).throw(AssertionError("MCP must not be re-registered")),
    )

    assert bundle_entrypoint.read_text(encoding="utf-8") == (package / "bin/session-handoff").read_text(encoding="utf-8")


def test_setup_rolls_back_all_changes_when_mcp_registration_fails(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    originals = {}
    for client in ("codex", "claude"):
        original = bin_dir / client
        original.write_text(f"{client}-original", encoding="utf-8")
        original.chmod(0o755)
        originals[client] = original
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[0].endswith("claude"):
            raise RuntimeError("MCP unavailable")

    try:
        install_setup(
            package,
            home,
            ["codex", "claude"],
            executable_paths=originals,
            runner=runner,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("setup failure must be propagated")

    assert originals["codex"].read_text(encoding="utf-8") == "codex-original"
    assert originals["claude"].read_text(encoding="utf-8") == "claude-original"
    assert not (bin_dir / "codex.session-handoff-original").exists()
    assert not (home / ".config/session-handoff/state.json").exists()
    assert not (home / ".codex/skills/session-handoff/SKILL.md").exists()
    assert not (home / ".claude/skills/session-handoff/SKILL.md").exists()


def test_setup_rollback_removes_a_prior_mcp_registration_before_launcher_move(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    originals = {}
    for client in ("codex", "claude"):
        original = bin_dir / client
        original.write_text(f"{client}-original", encoding="utf-8")
        original.chmod(0o755)
        originals[client] = original
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[0].endswith("claude") and argv[2] == "add":
            raise RuntimeError("MCP unavailable")

    try:
        install_setup(
            package,
            home,
            ["codex", "claude"],
            executable_paths=originals,
            runner=runner,
        )
    except RuntimeError:
        pass

    assert calls[-1] == [str(originals["codex"]), "mcp", "remove", "session-handoff"]


def test_setup_rollback_removes_mcp_using_the_original_executable(tmp_path, monkeypatch):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    launcher = home / ".local/bin/codex"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("codex-original", encoding="utf-8")
    launcher.chmod(0o755)
    calls = []

    def fail_state(*_args, **_kwargs):
        raise OSError("state write failed")

    monkeypatch.setattr(_setup_impl, "_write_json", fail_state)
    try:
        install_setup(
            package,
            home,
            ["codex"],
            executable_paths={"codex": launcher},
            runner=calls.append,
        )
    except OSError:
        pass
    else:
        raise AssertionError("state failure must be propagated")

    assert calls[-1] == [str(launcher), "mcp", "remove", "session-handoff"]


def test_setup_can_add_a_second_client_after_the_first(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    codex = bin_dir / "codex"
    claude = bin_dir / "claude"
    codex.write_text("codex-original", encoding="utf-8")
    claude.write_text("claude-original", encoding="utf-8")
    codex.chmod(0o755)
    claude.chmod(0o755)
    calls = []

    install_setup(package, home, ["codex"], executable_paths={"codex": codex}, runner=calls.append)
    install_setup(
        package,
        home,
        ["codex", "claude"],
        executable_paths={"codex": codex, "claude": claude},
        runner=calls.append,
    )

    assert (bin_dir / "codex.session-handoff-original").read_text(encoding="utf-8") == "codex-original"
    assert (bin_dir / "claude.session-handoff-original").read_text(encoding="utf-8") == "claude-original"
    assert json.loads((home / ".config/session-handoff/state.json").read_text(encoding="utf-8"))["clients"] == ["codex", "claude"]
    assert len(calls) == 2


def test_setup_reports_corrupt_state_as_a_setup_error(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    state = home / ".config/session-handoff/state.json"
    state.parent.mkdir(parents=True)
    state.write_text("not-json", encoding="utf-8")

    try:
        install_setup(
            package,
            home,
            ["codex"],
            executable_paths={"codex": home / ".local/bin/codex"},
            runner=lambda _: None,
        )
    except SetupError as exc:
        assert "state" in str(exc)
    else:
        raise AssertionError("corrupt state must be actionable")


def test_setup_rewraps_a_client_symlink_replaced_by_an_update(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    first = tmp_path / "codex-v1"
    second = tmp_path / "codex-v2"
    first.write_text("v1", encoding="utf-8")
    second.write_text("v2", encoding="utf-8")
    launcher = bin_dir / "codex"
    launcher.symlink_to(first)
    calls = []

    install_setup(package, home, ["codex"], executable_paths={"codex": launcher}, runner=calls.append)
    launcher.unlink()
    launcher.symlink_to(second)
    install_setup(package, home, ["codex"], executable_paths={"codex": launcher}, runner=calls.append)

    content = launcher.read_text(encoding="utf-8")
    assert "session-handoff-active" in content
    assert (bin_dir / "codex.session-handoff-original").is_symlink()
    assert len(calls) == 1


def test_restore_setup_returns_the_original_launcher_and_removes_managed_files(tmp_path):
    package = Path(__file__).parents[1]
    home = tmp_path / "home"
    launcher = home / ".local/bin/codex"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("original", encoding="utf-8")
    launcher.chmod(0o755)
    calls = []

    install_setup(package, home, ["codex"], executable_paths={"codex": launcher}, runner=calls.append)
    result = restore_setup(home, runner=calls.append)

    assert result["restored"] is True
    assert launcher.read_text(encoding="utf-8") == "original"
    assert not (home / ".config/session-handoff/state.json").exists()
    assert not (home / ".local/share/session-handoff/plugin").exists()
    assert not (home / ".codex/skills/session-handoff/SKILL.md").exists()
    assert calls[-1] == [str(launcher.with_name("codex.session-handoff-original")), "mcp", "remove", "session-handoff"]
