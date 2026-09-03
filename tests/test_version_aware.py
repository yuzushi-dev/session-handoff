import json
import subprocess
from pathlib import Path

import pytest

from benchmark.version_aware import (
    BuildSpecError,
    compare_observations,
    load_build,
    parse_build_spec,
    run_product_command,
    _snapshot_home,
    _tree_sha256,
)


def write_manifest(root: Path, version: str = "0.7.0") -> None:
    for relative in (
        "package.json",
        "plugin.json",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"name": "session-handoff", "version": version}),
            encoding="utf-8",
        )


def test_parse_build_spec_requires_nonempty_label_and_root(tmp_path):
    assert parse_build_spec(f"candidate={tmp_path}") == ("candidate", tmp_path.resolve())

    for value in ("candidate", "=root", "candidate="):
        with pytest.raises(BuildSpecError):
            parse_build_spec(value)


def test_load_build_records_manifest_versions_and_rejects_mismatch(tmp_path):
    write_manifest(tmp_path, "0.6.1")
    build = load_build("legacy", tmp_path)
    assert build.identity["package_version"] == "0.6.1"
    assert build.identity["manifest_versions"] == {
        "package.json": "0.6.1",
        "plugin.json": "0.6.1",
        ".claude-plugin/plugin.json": "0.6.1",
        ".codex-plugin/plugin.json": "0.6.1",
    }

    (tmp_path / "plugin.json").write_text(
        json.dumps({"name": "session-handoff", "version": "9.9.9"}),
        encoding="utf-8",
    )
    with pytest.raises(BuildSpecError, match="manifest versions"):
        load_build("broken", tmp_path)


def test_run_product_command_uses_build_root_and_drops_provider_environment(tmp_path, monkeypatch):
    write_manifest(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, pathlib\n"
        "print(json.dumps({'cwd': os.getcwd(), 'home': str(pathlib.Path.home()), 'keys': sorted(k for k in os.environ if 'API_KEY' in k)}))\n",
        encoding="utf-8",
    )
    home = tmp_path / "isolated-home"
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    result = run_product_command(
        load_build("candidate", tmp_path),
        "probe.py",
        home=home,
    )

    assert result["returncode"] == 0
    observed = json.loads(result["stdout"])
    assert observed["cwd"] == "<build>"
    assert observed["home"] == "<home>"
    assert observed["keys"] == []


def test_compare_observations_reports_field_level_difference():
    comparison = compare_observations(
        {"candidate-a": {"state": "unasked"}, "candidate-b": {"state": "asked"}}
    )
    assert comparison["status"] == "different"
    assert comparison["differences"] == [
        {"path": "/state", "values": {"candidate-a": "unasked", "candidate-b": "asked"}}
    ]


def test_snapshot_omits_telemetry_lock_internals(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".config/session-handoff/telemetry.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"schema_version": 1, "enabled": false}', encoding="utf-8")
    (config.parent / "telemetry.json.lock").write_text("1", encoding="utf-8")
    state = home / ".local/state/session-handoff"
    state.mkdir(parents=True)
    (state / "telemetry-generation").write_text("1", encoding="utf-8")

    snapshot = _snapshot_home(home, tmp_path / "build")

    assert [entry["path"] for entry in snapshot] == [
        ".config/session-handoff/telemetry.json"
    ]


def test_tree_hash_ignores_untracked_files_in_git_tree(tmp_path):
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("tracked", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    before = _tree_sha256(tmp_path)

    (tmp_path / "untracked.txt").write_text("noise", encoding="utf-8")

    assert _tree_sha256(tmp_path) == before
