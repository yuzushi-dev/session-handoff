import json
import platform
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import server.command_matrix as command_matrix
from server.command_matrix import probe_command_matrix
from server import handoff_store
from server.compatibility import content_hash
from server.setup import install_setup


ROOT = Path(__file__).parents[1]


def install_fixture(tmp_path):
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    executables = {}
    for client in ("codex", "claude"):
        executable = bin_dir / client
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        executables[client] = executable
    install_setup(
        ROOT,
        home,
        ["codex", "claude"],
        executable_paths=executables,
        runner=lambda _argv: None,
    )
    return home


def successful_runner(argv, **_kwargs):
    if "--version" in argv:
        return SimpleNamespace(returncode=0, stdout="test-client 1.0\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="configured\n", stderr="")


def _compatibility_report(cases, versions):
    return {
        "schema": "session-handoff.compatibility-report/v1",
        "build": {
            "package_version": json.loads((ROOT / "package.json").read_text())["version"],
            "source_kind": "checkout",
            "content_sha256": content_hash(ROOT),
        },
        "environment": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "clients": {
            client: {"present": True, "version": version}
            for client, version in versions.items()
        },
        "cases": cases,
    }


def _isolate_central_store(monkeypatch, root):
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))


def test_command_matrix_proves_all_four_provider_free_flows(
    tmp_path, tmp_path_factory, monkeypatch
):
    _isolate_central_store(monkeypatch, tmp_path_factory.mktemp("matrix-xdg"))
    home = install_fixture(tmp_path)

    result = probe_command_matrix(
        home,
        runner=successful_runner,
    )

    assert result["ready"] is True
    assert result["flows"] == {
        "claude_handoff": {"command": "/session-handoff", "ready": True},
        "codex_handoff": {"command": "$session-handoff", "ready": True},
        "claude_to_codex": {
            "command": "/session-handoff migrate codex",
            "ready": True,
        },
        "codex_to_claude": {
            "command": "$session-handoff migrate claude",
            "ready": True,
        },
    }
    assert str(tmp_path) not in json.dumps(result)


def test_doctor_separates_local_readiness_from_unverified_capacity(tmp_path, monkeypatch):
    _isolate_central_store(monkeypatch, tmp_path / "xdg")
    home = install_fixture(tmp_path)

    result = probe_command_matrix(home, runner=successful_runner)

    assert result["local_readiness"]["ready"] is True
    assert result["certification"] == {
        "status": "unverified",
        "reason": "no matching compatibility evidence",
        "capabilities": {
            "installation": "not-run",
            "handoff": "not-run",
            "migration_format": "not-run",
            "compaction": "not-run",
        },
        "tested_cases": [],
    }
    for client in ("codex", "claude"):
        status = result["clients"][client]
        assert status["version"] == "1.0"
        assert status["version_output"] == "test-client 1.0"
        assert status["installation_type"] == "path"
        assert status["executable_origin"] == "managed_target"
        assert status["launcher_status"] == "managed"
        assert status["certification"] == "unverified"


def test_doctor_reports_an_unmanaged_path_client_without_marking_it_ready(tmp_path, monkeypatch):
    executable = tmp_path / "bin/codex"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        command_matrix.shutil,
        "which",
        lambda name: str(executable) if name == "codex" else None,
    )

    result = probe_command_matrix(tmp_path / "home", runner=successful_runner)

    codex = result["clients"]["codex"]
    assert codex["managed"] is False
    assert codex["ready"] is False
    assert codex["version"] == "1.0"
    assert codex["installation_type"] == "path"
    assert codex["executable_origin"] == "path"
    assert codex["launcher_status"] == "unmanaged"


def test_doctor_client_probes_cannot_write_to_the_caller_home_or_xdg(tmp_path):
    home = install_fixture(tmp_path)
    codex_config = home / ".codex/config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    codex_config.write_text("[mcp_servers.session-handoff]\ncommand = 'python3'\n", encoding="utf-8")
    claude_config = home / ".claude.json"
    claude_config.write_text('{"mcpServers":{"session-handoff":{}}}\n', encoding="utf-8")
    before = {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    probe_homes = set()

    def mutating_runner(argv, **kwargs):
        env = kwargs["env"]
        probe_home = Path(env["HOME"])
        first_probe = probe_home not in probe_homes
        probe_homes.add(probe_home)
        assert probe_home != home
        assert env["XDG_CONFIG_HOME"].startswith(str(probe_home))
        assert env["XDG_STATE_HOME"].startswith(str(probe_home))
        if first_probe:
            assert (probe_home / ".codex/config.toml").read_text() == codex_config.read_text()
            assert (probe_home / ".claude.json").read_text() == claude_config.read_text()
        (probe_home / ".claude/backups").mkdir(parents=True, exist_ok=True)
        (probe_home / ".claude.json").write_text("mutated", encoding="utf-8")
        state = Path(env["XDG_STATE_HOME"]) / "client-state"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("created", encoding="utf-8")
        stdout = "codex-cli 0.159.2\n" if "--version" in argv else "configured\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    probe_command_matrix(home, runner=mutating_runner)

    after = {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert probe_homes
    assert all(not probe_home.exists() for probe_home in probe_homes)


def test_doctor_resolves_a_managed_path_wrapper_without_executing_it(tmp_path, monkeypatch):
    native = tmp_path / "bin/codex-native"
    native.parent.mkdir()
    native.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    native.chmod(0o755)
    wrapper = tmp_path / "bin/codex"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec python3 /plugin/bin/session-handoff run codex --executable {native} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    wrapper_before = wrapper.read_bytes()
    sibling = tmp_path / "bin/codex.session-handoff-original"
    sibling.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        command_matrix.shutil,
        "which",
        lambda name: str(wrapper) if name == "codex" else None,
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if Path(argv[0]) == wrapper:
            sibling.write_text("wrapper executed", encoding="utf-8")
            raise AssertionError("doctor must not execute a managed wrapper")
        stdout = "codex-cli 0.159.2\n" if "--version" in argv else "configured\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    result = probe_command_matrix(tmp_path / "empty-home", runner=runner)

    assert result["clients"]["codex"]["version"] == "0.159.2"
    assert calls and all(Path(argv[0]) == native for argv in calls)
    assert wrapper.read_bytes() == wrapper_before
    assert sibling.read_text(encoding="utf-8") == "preserve"


def test_doctor_fails_closed_on_a_malformed_managed_path_wrapper(tmp_path, monkeypatch):
    wrapper = tmp_path / "bin/codex"
    wrapper.parent.mkdir()
    wrapper.write_text(
        "#!/bin/sh\nexec python3 /plugin/bin/session-handoff run codex --executable\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(
        command_matrix.shutil,
        "which",
        lambda name: str(wrapper) if name == "codex" else None,
    )
    calls = []

    result = probe_command_matrix(
        tmp_path / "empty-home",
        runner=lambda argv, **kwargs: calls.append(argv),
    )

    assert result["clients"]["codex"]["executable"] is False
    assert calls == []


def test_probe_sandbox_is_removed_when_config_copy_fails(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("config", encoding="utf-8")
    created = []
    real_temporary_directory = command_matrix.tempfile.TemporaryDirectory

    def temporary_directory(*args, **kwargs):
        value = real_temporary_directory(*args, **kwargs)
        created.append(Path(value.name))
        return value

    monkeypatch.setattr(command_matrix.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(
        command_matrix.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
    )

    with pytest.raises(OSError, match="copy failed"):
        command_matrix._isolated_probe_environment(home)

    assert created and all(not path.exists() for path in created)


def test_unknown_client_version_is_unverified_not_incompatible(tmp_path):
    home = install_fixture(tmp_path)

    def runner(argv, **kwargs):
        if "--version" in argv:
            return SimpleNamespace(returncode=0, stdout="codex 999.0.0\n", stderr="")
        return successful_runner(argv, **kwargs)

    result = probe_command_matrix(home, runner=runner)

    assert result["clients"]["codex"]["certification"] == "unverified"
    assert "incompatible" not in json.dumps(result)


def test_doctor_certifies_only_live_cases_for_exact_client_versions(tmp_path):
    home = install_fixture(tmp_path)
    report = tmp_path / "compatibility-report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "session-handoff.compatibility-report/v1",
                "build": {
                    "package_version": json.loads((ROOT / "package.json").read_text())["version"],
                    "source_kind": "checkout",
                    "content_sha256": content_hash(ROOT),
                },
                "environment": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                },
                "clients": {
                    "codex": {"present": True, "version": "1.0"},
                    "claude": {"present": True, "version": "1.0"},
                },
                "cases": [
                    {
                        "id": "codex-handoff-live",
                        "capability": "handoff",
                        "proof_level": "live",
                        "clients": ["codex"],
                        "client_versions": {"codex": "1.0"},
                        "status": "passed",
                    },
                    {
                        "id": "migration-live",
                        "capability": "migration_format",
                        "proof_level": "live",
                        "clients": ["codex", "claude"],
                        "client_versions": {"codex": "1.0", "claude": "1.0"},
                        "status": "passed",
                    },
                    {
                        "id": "ignored-offline",
                        "capability": "handoff",
                        "proof_level": "deterministic",
                        "clients": ["claude"],
                        "status": "failed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = probe_command_matrix(
        home,
        runner=successful_runner,
        compatibility_report=report,
    )

    assert result["certification"]["status"] == "unverified"
    assert result["certification"]["reason"] == "required live compatibility matrix is incomplete"
    assert result["certification"]["capabilities"] == {
        "installation": "not-run",
        "handoff": "passed",
        "migration_format": "passed",
        "compaction": "not-run",
    }
    assert result["clients"]["codex"]["certification"] == "unverified"
    assert result["clients"]["claude"]["certification"] == "unverified"


def test_failed_live_case_dominates_client_certification_regardless_of_order(tmp_path):
    home = install_fixture(tmp_path)
    report = tmp_path / "compatibility-report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "session-handoff.compatibility-report/v1",
                "build": {
                    "package_version": json.loads((ROOT / "package.json").read_text())["version"],
                    "source_kind": "checkout",
                    "content_sha256": content_hash(ROOT),
                },
                "environment": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                },
                "clients": {
                    "codex": {"present": True, "version": "1.0"},
                    "claude": {"present": True, "version": "1.0"},
                },
                "cases": [
                    {"id": "codex-to-claude-migration", "capability": "migration", "proof_level": "live", "clients": ["codex", "claude"], "client_versions": {"codex": "1.0", "claude": "1.0"}, "status": "failed"},
                    {"id": "codex-handoff", "capability": "handoff", "proof_level": "live", "clients": ["codex"], "client_versions": {"codex": "1.0"}, "status": "passed"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = probe_command_matrix(home, runner=successful_runner, compatibility_report=report)

    assert result["certification"]["status"] == "failed"
    assert result["clients"]["codex"]["certification"] == "failed"
    assert result["clients"]["claude"]["certification"] == "failed"


def test_doctor_rejects_live_evidence_for_a_different_build(tmp_path):
    home = install_fixture(tmp_path)
    report = tmp_path / "compatibility-report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "session-handoff.compatibility-report/v1",
                "build": {
                    "package_version": json.loads((ROOT / "package.json").read_text())["version"],
                    "source_kind": "checkout",
                    "content_sha256": "0" * 64,
                },
                "environment": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                },
                "clients": {
                    "codex": {"present": True, "version": "1.0"},
                    "claude": {"present": True, "version": "1.0"},
                },
                "cases": [
                    {"capability": "handoff", "proof_level": "live", "clients": ["codex"], "status": "passed"},
                    {"capability": "migration", "proof_level": "live", "clients": ["codex", "claude"], "status": "passed"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = probe_command_matrix(home, runner=successful_runner, compatibility_report=report)

    assert result["certification"]["status"] == "unverified"
    assert result["certification"]["reason"] == "compatibility evidence is for a different build or platform"


def test_doctor_verifies_only_the_complete_required_live_matrix(tmp_path):
    home = install_fixture(tmp_path)
    versions = {"codex": "1.0", "claude": "1.0"}
    cases = []
    for case_id, (capability, required_clients) in command_matrix.REQUIRED_LIVE_CASES.items():
        clients = [client for client in ("codex", "claude") if client in required_clients]
        cases.append(
            {
                "id": case_id,
                "capability": "migration" if capability == "migration_format" else capability,
                "proof_level": "live",
                "clients": clients,
                "client_versions": {client: versions[client] for client in clients},
                "status": "passed",
            }
        )
    report = tmp_path / "compatibility-report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "session-handoff.compatibility-report/v1",
                "build": {
                    "package_version": json.loads((ROOT / "package.json").read_text())["version"],
                    "source_kind": "checkout",
                    "content_sha256": content_hash(ROOT),
                },
                "environment": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                },
                "clients": {
                    client: {"present": True, "version": version}
                    for client, version in versions.items()
                },
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )

    result = probe_command_matrix(home, runner=successful_runner, compatibility_report=report)

    assert result["certification"]["status"] == "verified"
    assert len(result["certification"]["tested_cases"]) == len(command_matrix.REQUIRED_LIVE_CASES)
    assert result["clients"]["codex"]["certification"] == "verified"
    assert result["clients"]["claude"]["certification"] == "verified"


def test_failed_extra_live_case_blocks_an_otherwise_complete_matrix(tmp_path):
    home = install_fixture(tmp_path)
    versions = {"codex": "1.0", "claude": "1.0"}
    cases = [
        {
            "id": case_id,
            "capability": "migration" if capability == "migration_format" else capability,
            "proof_level": "live",
            "clients": [client for client in ("codex", "claude") if client in required_clients],
            "client_versions": {
                client: versions[client]
                for client in ("codex", "claude")
                if client in required_clients
            },
            "status": "passed",
        }
        for case_id, (capability, required_clients) in command_matrix.REQUIRED_LIVE_CASES.items()
    ]
    cases.append(
        {
            "id": "handoff-retry",
            "capability": "handoff",
            "proof_level": "live",
            "clients": ["codex"],
            "client_versions": {"codex": "1.0"},
            "status": "failed",
        }
    )
    report = tmp_path / "compatibility-report.json"
    report.write_text(json.dumps(_compatibility_report(cases, versions)), encoding="utf-8")

    result = probe_command_matrix(home, runner=successful_runner, compatibility_report=report)

    assert result["certification"]["status"] == "failed"


def test_duplicate_live_case_ids_make_certification_unverified(tmp_path):
    home = install_fixture(tmp_path)
    versions = {"codex": "1.0", "claude": "1.0"}
    case = {
        "id": "codex-handoff",
        "capability": "handoff",
        "proof_level": "live",
        "clients": ["codex"],
        "client_versions": {"codex": "1.0"},
        "status": "passed",
    }
    report = tmp_path / "compatibility-report.json"
    report.write_text(json.dumps(_compatibility_report([case, case], versions)), encoding="utf-8")

    result = probe_command_matrix(home, runner=successful_runner, compatibility_report=report)

    assert result["certification"]["status"] == "unverified"
    assert result["certification"]["reason"] == "invalid compatibility evidence"


def test_command_matrix_fails_closed_when_one_mcp_registration_is_missing(tmp_path):
    home = install_fixture(tmp_path)

    def runner(argv, **kwargs):
        if Path(argv[0]).name.startswith("claude") and "mcp" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        return successful_runner(argv, **kwargs)

    result = probe_command_matrix(
        home,
        runner=runner,
    )

    assert result["ready"] is False
    assert result["clients"]["claude"]["mcp"] is False
    assert result["flows"]["claude_handoff"]["ready"] is False
    assert result["flows"]["codex_handoff"]["ready"] is True
    assert result["flows"]["claude_to_codex"]["ready"] is False
    assert result["flows"]["codex_to_claude"]["ready"] is False


def test_doctor_cli_emits_the_same_content_free_matrix(
    tmp_path, tmp_path_factory, monkeypatch
):
    _isolate_central_store(monkeypatch, tmp_path_factory.mktemp("doctor-xdg"))
    home = install_fixture(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin/session-handoff"),
            "doctor",
            "--home",
            str(home),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert payload["provider_calls"] == 0
    assert str(tmp_path) not in result.stdout


def test_command_matrix_supports_direct_module_help():
    result = subprocess.run(
        [sys.executable, str(ROOT / "server/command_matrix.py"), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Check handoff and migrate readiness" in result.stdout


def test_doctor_reports_absent_central_store_without_creating_it(tmp_path, monkeypatch):
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    result = probe_command_matrix(tmp_path / "home", runner=successful_runner)

    health = result["central_store"]
    assert health["status"] == "absent"
    assert health["data_root"]["status"] == "absent"
    assert health["state_root"]["status"] == "absent"
    assert health["catalog"]["status"] == "absent"
    assert health["data_root"]["writable"] is True
    assert health["state_root"]["writable"] is True
    assert not data_home.exists() and not state_home.exists()


def test_doctor_reports_healthy_central_store_and_catalog(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    record = handoff_store.create_record(str(workspace), "one.md", "one")
    handoff_store.list_records(str(workspace))

    health = command_matrix.probe_central_store()

    assert health["status"] == "healthy"
    assert health["data_root"]["status"] == "healthy"
    assert health["state_root"]["status"] == "healthy"
    assert health["catalog"]["status"] == "healthy"
    assert health["catalog"]["path"].endswith("catalog.sqlite3")
    assert record["project_id"]


def test_doctor_distinguishes_unsafe_root_and_corrupt_catalog(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    handoff_store.create_record(str(workspace), "one.md", "one")
    handoff_store.list_records(str(workspace))

    data_root = handoff_store.data_root()
    data_root.chmod(0o777)
    unsafe = command_matrix.probe_central_store()
    data_root.chmod(0o700)
    handoff_store.catalog_path().write_bytes(b"not sqlite")

    corrupt = command_matrix.probe_central_store()

    assert unsafe["data_root"]["status"] == "unsafe"
    assert unsafe["status"] == "unsafe"
    assert corrupt["catalog"]["status"] == "corrupt"
    assert corrupt["status"] == "corrupt"


@pytest.mark.parametrize(
    ("status", "writable", "ready"),
    [
        ("absent", True, True),
        ("healthy", True, True),
        ("unsafe", False, False),
        ("corrupt", False, False),
        ("unwritable", False, False),
        ("healthy", False, False),
    ],
)
def test_doctor_readiness_includes_central_health(tmp_path, monkeypatch, status, writable, ready):
    home = install_fixture(tmp_path)
    monkeypatch.setattr(
        command_matrix,
        "probe_central_store",
        lambda: {"status": status, "writable": writable},
    )

    result = probe_command_matrix(home, runner=successful_runner)

    assert result["ready"] is ready


def test_doctor_reports_read_only_catalog_as_unwritable(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    handoff_store.create_record(str(workspace), "one.md", "one")
    handoff_store.list_records(str(workspace))
    catalog = handoff_store.catalog_path()
    catalog.chmod(0o400)

    health = command_matrix.probe_central_store()

    assert health["catalog"]["status"] == "unwritable"
    assert health["catalog"]["writable"] is False
    assert health["status"] == "unwritable"
    assert health["writable"] is False


def test_probe_compaction_scoring_reports_ollama_not_installed(monkeypatch):
    monkeypatch.setattr(command_matrix.shutil, "which", lambda name: None)
    status = command_matrix.probe_compaction_scoring(model="smollm2:1.7b")
    assert status == {"installed": False, "reachable": False, "model_pulled": False}


def test_probe_compaction_scoring_reports_installed_but_unreachable(monkeypatch):
    monkeypatch.setattr(command_matrix.shutil, "which", lambda name: "/usr/local/bin/ollama")

    def fake_urlopen(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(command_matrix, "urlopen", fake_urlopen)
    status = command_matrix.probe_compaction_scoring(model="smollm2:1.7b")
    assert status == {"installed": True, "reachable": False, "model_pulled": False}


def test_probe_compaction_scoring_reports_model_pulled(monkeypatch):
    monkeypatch.setattr(command_matrix.shutil, "which", lambda name: "/usr/local/bin/ollama")

    class _Resp:
        def read(self):
            return json.dumps({"models": [{"name": "smollm2:1.7b"}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(command_matrix, "urlopen", lambda url, timeout: _Resp())
    status = command_matrix.probe_compaction_scoring(model="smollm2:1.7b")
    assert status == {"installed": True, "reachable": True, "model_pulled": True}


def test_probe_compaction_scoring_handles_non_dict_json_response(monkeypatch):
    monkeypatch.setattr(command_matrix.shutil, "which", lambda name: "/usr/local/bin/ollama")

    class _Resp:
        def read(self):
            return json.dumps([1, 2, 3]).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(command_matrix, "urlopen", lambda url, timeout: _Resp())
    status = command_matrix.probe_compaction_scoring(model="smollm2:1.7b")
    assert status == {"installed": True, "reachable": True, "model_pulled": False}


def test_probe_typesafe_scoring_reports_unconfigured(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        "server.typesafe_client.TypeSafeClient._resolve_api_key",
        staticmethod(lambda: None),
    )
    assert command_matrix.probe_typesafe_scoring() == {"configured": False}


def test_probe_typesafe_scoring_reports_configured_via_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-value")
    assert command_matrix.probe_typesafe_scoring() == {"configured": True}


def test_probe_typesafe_scoring_makes_no_network_call(monkeypatch):
    # doctor's own contract is provider_calls: 0; TypeSafe is a real remote
    # provider, unlike Ollama's local /api/tags check, so this must never
    # attempt a request regardless of configuration state.
    def _forbidden(*args, **kwargs):
        raise AssertionError("probe_typesafe_scoring must not make network calls")

    monkeypatch.setattr("server.typesafe_client.urllib.request.urlopen", _forbidden)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-value")
    command_matrix.probe_typesafe_scoring()


def test_doctor_human_mode_is_opt_in_and_keeps_json_default(tmp_path):
    human = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin/session-handoff"),
            "doctor",
            "--home",
            str(tmp_path / "home"),
            "--human",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert human.stdout.startswith("Session-handoff doctor:")
    assert "central store:" in human.stdout
