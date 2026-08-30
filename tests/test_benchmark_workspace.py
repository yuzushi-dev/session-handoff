import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark.fixture_workspace import CASE_IDS, materialize_workspace


ROOT = Path(__file__).parents[1]
REFERENCE_FIXES = {
    "buried-constraint": (
        "src/auth/session.py",
        "return bool(token.get('access_valid'))",
        "return bool(token.get('access_valid') or token.get('refresh_valid'))",
    ),
    "superseded-decision": (
        "tests/cache/test_negative_ttl.py",
        "assert NEGATIVE_CACHE_TTL == 60",
        "assert NEGATIVE_CACHE_TTL == 15",
    ),
    "failed-attempt-trap": (
        "src/slug/normalize.py",
        "import re\n",
        "import re\nimport unicodedata\n",
    ),
    "partial-state": (
        "src/codec/reader.py",
        "payload.get('version') != 3",
        "payload.get('version') not in {2, 3}",
    ),
    "late-correction": (
        "src/client/export.py",
        "/api/v1/export",
        "/api/v2/exports",
    ),
    "compound-rot": (
        "src/retry/policy.py",
        "TRANSIENT_ERRORS = (ConnectionError,)",
        "TRANSIENT_ERRORS = (ConnectionError, TimeoutError)",
    ),
}


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_fixture_workspace_starts_at_the_intended_failure(tmp_path, case_id):
    workspace = tmp_path / case_id
    fixture = materialize_workspace(case_id, workspace)

    result = subprocess.run(
        fixture["verify_command"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert fixture["expected_failure"] in result.stdout
    assert "ERROR" not in result.stdout


def test_prepare_study_records_one_workspace_template_per_case(tmp_path):
    output = tmp_path / "study"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/prepare_study.py"),
            str(ROOT / "benchmark/fixtures/context_rot_cases.json"),
            "--output",
            str(output),
            "--runs-per-condition",
            "1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evaluation = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    templates = {run["workspace_template"] for run in evaluation["runs"]}
    assert templates == {f"{case_id}/workspace" for case_id in CASE_IDS}
    assert {
        run["handoff_format"]
        for run in evaluation["runs"]
        if run["condition"] == "handoff"
    } == {"markdown-v1", "state-v1"}
    for template in templates:
        assert (output / template).is_dir()
    assert all(run["verify_command"][0:2] == ["python3", "-c"] for run in evaluation["runs"])
    assert all(run["acceptance_command"][0:2] == ["python3", "-c"] for run in evaluation["runs"])


def test_hidden_acceptance_rejects_a_coherently_stale_fix(tmp_path):
    workspace = tmp_path / "superseded-decision"
    fixture = materialize_workspace("superseded-decision", workspace)
    (workspace / "cache/config.py").write_text(
        "NEGATIVE_CACHE_TTL = 60\n", encoding="utf-8"
    )

    visible = subprocess.run(fixture["verify_command"], cwd=workspace, check=False)
    hidden = subprocess.run(fixture["acceptance_command"], cwd=workspace, check=False)

    assert visible.returncode == 0
    assert hidden.returncode != 0


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_fixture_definition_of_done_is_executable(tmp_path, case_id):
    workspace = tmp_path / case_id
    fixture = materialize_workspace(case_id, workspace)
    relative, old, new = REFERENCE_FIXES[case_id]
    target = workspace / relative
    content = target.read_text(encoding="utf-8")
    assert old in content
    target.write_text(content.replace(old, new), encoding="utf-8")
    if case_id == "failed-attempt-trap":
        content = target.read_text(encoding="utf-8")
        target.write_text(
            content.replace("value.lower()", "unicodedata.normalize('NFC', value).lower()"),
            encoding="utf-8",
        )

    result = subprocess.run(
        fixture["verify_command"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout
