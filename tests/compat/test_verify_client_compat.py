import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/verify_client_compat.py"
FIXTURES = ROOT / "tests/compat/fixtures"


def load_runner():
    spec = importlib.util.spec_from_file_location("verify_client_compat", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_runner(tmp_path, *extra, path="/usr/bin:/bin"):
    output = tmp_path / "state/compatibility-report.json"
    env = {
        "HOME": str(tmp_path / "caller-home"),
        "PATH": path,
        "LANG": "C.UTF-8",
        "OPENAI_API_KEY": "must-not-be-inherited",
    }
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--fixtures",
            str(FIXTURES),
            "--output",
            str(output),
            *extra,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    return result, output, json.loads(output.read_text(encoding="utf-8"))


def test_offline_report_identifies_build_and_isolated_lab(tmp_path):
    result, output, report = run_runner(tmp_path)

    assert result.returncode == 0, result.stderr
    assert report["schema"] == "session-handoff.compatibility-report/v1"
    assert report["build"]["package_version"] == json.loads(
        (ROOT / "package.json").read_text()
    )["version"]
    assert report["build"]["source_kind"] == "checkout"
    assert report["build"]["git_sha"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    assert report["build"]["git_tree"]
    assert len(report["build"]["content_sha256"]) == 64
    assert Path(report["lab"]["home"]).parent.parent == output.parent
    assert Path(report["lab"]["repository"]).is_dir()
    assert (Path(report["lab"]["repository"]) / ".git").is_dir()
    assert report["lab"]["inherited_env"] == ["LANG", "PATH"]
    assert "must-not-be-inherited" not in output.read_text(encoding="utf-8")


def test_missing_clients_are_not_run_and_never_passed(tmp_path):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    _, _, report = run_runner(tmp_path, path=str(empty_bin))

    for client in ("codex", "claude"):
        assert report["clients"][client]["present"] is False
        assert report["clients"][client]["authenticated"]["status"] == "not-run"
    live = [case for case in report["cases"] if case["proof_level"] == "live"]
    assert live
    assert {case["status"] for case in live} == {"not-run"}


def test_present_client_remains_distinct_from_authentication(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    client = fake_bin / "codex"
    client.write_text("#!/bin/sh\necho 'codex-cli 0.159.2'\n", encoding="utf-8")
    client.chmod(0o755)

    _, _, report = run_runner(tmp_path, path=str(fake_bin))

    codex = report["clients"]["codex"]
    assert codex["present"] is True
    assert codex["version"] == "0.159.2"
    assert codex["authenticated"]["status"] == "not-run"
    assert "authentication" in codex["authenticated"]["reason"]


def test_all_contract_fixtures_declare_provenance_and_anonymization():
    runner = load_runner()
    fixtures = runner.load_fixtures(FIXTURES)

    assert {item["event"] for item in fixtures} == {
        "start",
        "resume",
        "compact-manual",
        "compact-automatic",
        "stop",
        "setup",
        "handoff",
        "migration",
    }
    for fixture in fixtures:
        assert fixture["provenance"]["kind"] in {"synthetic", "native-capture"}
        assert fixture["anonymization"]["transformations"]
        if fixture["provenance"]["kind"] == "synthetic":
            assert fixture["provenance"]["captured"] is False


def test_live_evidence_is_imported_only_with_existing_relative_evidence(tmp_path):
    runner = load_runner()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / "resume.txt").write_text("synthetic native proof\n", encoding="utf-8")
    live = evidence_root / "live.json"
    live.write_text(
        json.dumps(
            {
                "schema": "session-handoff.live-evidence/v1",
                "build": {
                    key: runner.build_identity(ROOT)[key]
                    for key in ("package_version", "content_sha256")
                },
                "environment": {
                    "system": runner.platform.system(),
                    "release": runner.platform.release(),
                    "machine": runner.platform.machine(),
                },
                "cases": [
                    {
                        "id": "codex-native-resume",
                        "capability": "resume",
                        "clients": ["codex"],
                        "client_versions": {"codex": "0.159.2"},
                        "status": "passed",
                        "reason": "client resumed; OPENAI_API_KEY=sk-1234567890abcdef",
                        "evidence_files": ["resume.txt"],
                    },
                    {
                        "id": "codex-authentication",
                        "capability": "authentication",
                        "clients": ["codex"],
                        "client_versions": {"codex": "0.159.2"},
                        "status": "passed",
                        "reason": "an authorized synthetic model request completed",
                        "evidence_files": ["resume.txt"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result, _, report = run_runner(tmp_path / "run", "--live-evidence", str(live))

    assert result.returncode == 0, result.stderr
    case = next(item for item in report["cases"] if item["id"] == "codex-native-resume")
    assert case["proof_level"] == "live"
    assert case["status"] == "passed"
    assert case["evidence_files"] == [str((evidence_root / "resume.txt").resolve())]
    assert case["reason"] == "client resumed; OPENAI_API_KEY=[REDACTED]"
    assert "sk-1234567890abcdef" not in json.dumps(report)
    assert case["verified_against"]["build"] == {
        key: report["build"][key] for key in ("package_version", "content_sha256")
    }
    assert report["clients"]["codex"]["authenticated"]["status"] == "passed"
    assert report["clients"]["claude"]["authenticated"]["status"] == "not-run"


@pytest.mark.parametrize("bad_path", ["../secret.txt", "/tmp/secret.txt"])
def test_live_evidence_rejects_paths_outside_its_directory(tmp_path, bad_path):
    runner = load_runner()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    live = evidence_root / "live.json"
    live.write_text(
        json.dumps(
            {
                "schema": "session-handoff.live-evidence/v1",
                "build": {
                    key: runner.build_identity(ROOT)[key]
                    for key in ("package_version", "content_sha256")
                },
                "environment": {
                    "system": runner.platform.system(),
                    "release": runner.platform.release(),
                    "machine": runner.platform.machine(),
                },
                "cases": [
                    {
                        "id": "escape",
                        "capability": "resume",
                        "clients": ["codex"],
                        "client_versions": {"codex": "0.159.2"},
                        "status": "passed",
                        "reason": "bad evidence",
                        "evidence_files": [bad_path],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result, _, report = run_runner(tmp_path / "run", "--live-evidence", str(live))

    assert result.returncode == 1
    imported = next(item for item in report["cases"] if item["id"] == "live-evidence-import")
    assert imported["status"] == "failed"


def test_compatibility_runner_and_documentation_are_packaged():
    files = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"]

    assert "scripts/verify_client_compat.py" in files
    assert "docs/compatibility.md" in files
    assert "tests/compat/fixtures/" in files


def test_live_evidence_for_another_build_is_rejected(tmp_path):
    runner = load_runner()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    live = evidence_root / "live.json"
    live.write_text(
        json.dumps(
            {
                "schema": "session-handoff.live-evidence/v1",
                "build": {"package_version": "0.0.0", "content_sha256": "0" * 64},
                "environment": {
                    "system": runner.platform.system(),
                    "release": runner.platform.release(),
                    "machine": runner.platform.machine(),
                },
                "cases": [],
            }
        ),
        encoding="utf-8",
    )

    result, _, report = run_runner(tmp_path / "run", "--live-evidence", str(live))

    assert result.returncode == 1
    imported = next(item for item in report["cases"] if item["id"] == "live-evidence-import")
    assert imported["status"] == "failed"
    assert "different build" in imported["reason"]


def test_shared_content_hash_matches_runner():
    from server.compatibility import content_hash

    assert content_hash(ROOT) == load_runner().build_identity(ROOT)["content_sha256"]


@pytest.mark.parametrize(
    "banner,expected",
    [
        ("codex-cli 0.159.2", "0.159.2"),
        ("2.1.285 (Claude Code)", "2.1.285"),
        ("client version 0.159.2-dev.1+build", "0.159.2-dev.1+build"),
        ("unknown", None),
    ],
)
def test_shared_version_normalizer_handles_realistic_banners(banner, expected):
    from server.compatibility import normalize_version

    assert normalize_version(banner) == expected


def test_lab_output_cannot_mutate_the_package_tree():
    runner = load_runner()
    args = runner.parse_args(
        ["--root", str(ROOT), "--output", str(ROOT / "compatibility-report.json")]
    )

    with pytest.raises(runner.ReceiptError, match="outside the package root"):
        runner.make_report(args)


def test_runner_and_doctor_share_the_canonical_live_case_matrix():
    from server import command_matrix
    from server.compatibility import LIVE_CASE_REQUIREMENTS

    runner = load_runner()
    clients = {name: {"version": "test"} for name in ("codex", "claude")}
    runner_cases = {
        case["id"]: (case["capability"], frozenset(case["clients"]))
        for case in runner._not_run_live_cases(clients)
    }

    assert LIVE_CASE_REQUIREMENTS["claude-handoff"] == (
        "handoff", frozenset(("claude",))
    )
    assert LIVE_CASE_REQUIREMENTS["codex-handoff"] == (
        "handoff", frozenset(("codex",))
    )
    assert runner_cases == LIVE_CASE_REQUIREMENTS
    assert command_matrix.REQUIRED_LIVE_CASES == {
        case_id: (command_matrix.CAPABILITIES[capability], required_clients)
        for case_id, (capability, required_clients) in LIVE_CASE_REQUIREMENTS.items()
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: None,
        lambda document: [],
        lambda document: {**document, "cases": [None]},
        lambda document: {**document, "cases": ["not-an-object"]},
        lambda document: {
            **document,
            "cases": [{**document["cases"][0], "evidence_files": "proof.txt"}],
        },
        lambda document: {
            **document,
            "cases": [{**document["cases"][0], "client_versions": []}],
        },
        lambda document: {
            **document,
            "cases": [{**document["cases"][0], "clients": ["unknown-client"]}],
        },
    ],
)
def test_malformed_live_evidence_becomes_failed_receipt(tmp_path, mutate):
    runner = load_runner()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / "proof.txt").write_text("proof\n", encoding="utf-8")
    base = {
        "schema": "session-handoff.live-evidence/v1",
        "build": {
            key: runner.build_identity(ROOT)[key]
            for key in ("package_version", "content_sha256")
        },
        "environment": {
            "system": runner.platform.system(),
            "release": runner.platform.release(),
            "machine": runner.platform.machine(),
        },
        "cases": [
            {
                "id": "malformed-case",
                "capability": "resume",
                "clients": ["codex"],
                "client_versions": {"codex": "0.159.2"},
                "status": "passed",
                "reason": "synthetic proof",
                "evidence_files": ["proof.txt"],
            }
        ],
    }
    live = evidence_root / "live.json"
    live.write_text(json.dumps(mutate(base)), encoding="utf-8")

    result, _, report = run_runner(tmp_path / "run", "--live-evidence", str(live))

    assert result.returncode == 1
    assert not result.stderr
    imported = next(item for item in report["cases"] if item["id"] == "live-evidence-import")
    assert imported["status"] == "failed"
