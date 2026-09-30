#!/usr/bin/env python3
"""Create a local, evidence-backed client compatibility receipt."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from server.compatibility import LIVE_CASE_REQUIREMENTS, content_hash, normalize_version
from server.redaction import redact_value


REPORT_SCHEMA = "session-handoff.compatibility-report/v1"
FIXTURE_SCHEMA = "session-handoff.contract-fixture/v1"
LIVE_SCHEMA = "session-handoff.live-evidence/v1"
STATUSES = {"passed", "failed", "not-run"}
PROOF_LEVELS = {"deterministic", "simulated", "live"}
INHERITED_ENV = ("LANG", "LC_ALL", "PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
CLIENTS = ("codex", "claude")


class ReceiptError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_executable() -> str | None:
    git = shutil.which("git")
    if git is None:
        git = next((item for item in ("/usr/bin/git", "/bin/git") if Path(item).is_file()), None)
    return git


def _run_git(root: Path, *args: str) -> str | None:
    git = _git_executable()
    if git is None or not (root / ".git").exists():
        return None
    result = subprocess.run(
        [git, *args], cwd=root, text=True, capture_output=True, check=False, timeout=10
    )
    return result.stdout.strip() if result.returncode == 0 else None


def build_identity(root: Path) -> dict[str, Any]:
    package_path = root / "package.json"
    if not package_path.is_file():
        raise ReceiptError(f"package.json not found under {root}")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    git_sha = _run_git(root, "rev-parse", "HEAD")
    dirty_output = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "package_version": package["version"],
        "source_kind": "checkout" if (root / ".git").exists() else "installed-package",
        "git_sha": git_sha,
        "git_tree": _run_git(root, "rev-parse", "HEAD^{tree}"),
        "git_dirty": bool(dirty_output) if dirty_output is not None else None,
        "content_sha256": content_hash(root),
    }


def load_fixtures(directory: Path) -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("schema") != FIXTURE_SCHEMA:
            raise ReceiptError(f"{path.name}: unsupported fixture schema")
        for field in ("client", "client_version", "event", "provenance", "anonymization", "payload"):
            if field not in item:
                raise ReceiptError(f"{path.name}: missing {field}")
        provenance = item["provenance"]
        if provenance.get("kind") not in {"synthetic", "native-capture"}:
            raise ReceiptError(f"{path.name}: invalid provenance kind")
        if provenance["kind"] == "synthetic" and provenance.get("captured") is not False:
            raise ReceiptError(f"{path.name}: synthetic fixture marked as captured")
        transformations = item["anonymization"].get("transformations")
        if not isinstance(transformations, list) or not transformations:
            raise ReceiptError(f"{path.name}: anonymization transformations are required")
        item["_path"] = path.resolve()
        fixtures.append(item)
    if not fixtures:
        raise ReceiptError(f"no contract fixtures found under {directory}")
    return fixtures


def _lab_environment(base: Path) -> tuple[dict[str, str], dict[str, Any]]:
    inherited = {key: os.environ[key] for key in INHERITED_ENV if os.environ.get(key)}
    lab = base / "compat-lab"
    paths = {
        "home": lab / "home",
        "xdg_config_home": lab / "xdg/config",
        "xdg_state_home": lab / "xdg/state",
        "xdg_cache_home": lab / "xdg/cache",
        "xdg_data_home": lab / "xdg/data",
        "repository": lab / "repository",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    env = {
        **inherited,
        "HOME": str(paths["home"]),
        "XDG_CONFIG_HOME": str(paths["xdg_config_home"]),
        "XDG_STATE_HOME": str(paths["xdg_state_home"]),
        "XDG_CACHE_HOME": str(paths["xdg_cache_home"]),
        "XDG_DATA_HOME": str(paths["xdg_data_home"]),
        "SESSION_HANDOFF_HOME": str(paths["home"]),
    }
    repository = paths["repository"]
    if not (repository / ".git").is_dir():
        git = _git_executable()
        if git is None:
            raise ReceiptError("git is required to create the synthetic repository")
        result = subprocess.run(
            [git, "init", "--quiet", str(repository)], env=env, text=True,
            capture_output=True, check=False, timeout=15,
        )
        if result.returncode != 0:
            raise ReceiptError(f"cannot initialize synthetic repository: {result.stderr.strip()}")
    return env, {**{key: str(value) for key, value in paths.items()}, "inherited_env": sorted(inherited)}


def _client_details(name: str, env: dict[str, str]) -> dict[str, Any]:
    executable = shutil.which(name, path=env.get("PATH"))
    if executable is None:
        return {
            "present": False,
            "executable": None,
            "realpath": None,
            "version": None,
            "install_type": None,
            "authenticated": {
                "status": "not-run",
                "reason": "client executable is absent",
                "evidence_files": [],
            },
        }
    resolved = str(Path(executable).resolve())
    try:
        result = subprocess.run(
            [executable, "--version"], env=env, text=True, capture_output=True,
            check=False, timeout=15,
        )
        combined = "\n".join((result.stdout, result.stderr))
        version = normalize_version(combined)
    except (OSError, subprocess.TimeoutExpired):
        version = None
    if "/node_modules/" in resolved or "/npm/" in resolved:
        install_type = "npm"
    elif Path(executable).is_symlink():
        install_type = "symlink"
    else:
        install_type = "path"
    return {
        "present": True,
        "executable": executable,
        "realpath": resolved,
        "version": version,
        "install_type": install_type,
        "authenticated": {
            "status": "not-run",
            "reason": "authentication was not probed; offline verification never starts a model",
            "evidence_files": [],
        },
    }


def _evidence_hashes(paths: list[str]) -> dict[str, str]:
    return {path: _sha256(Path(path)) for path in paths}


def _fixture_cases(fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = []
    for item in fixtures:
        path = str(item.pop("_path"))
        cases.append({
            "id": f"fixture-{item['event']}",
            "capability": item["event"],
            "proof_level": "deterministic",
            "clients": [item["client"]],
            "client_versions": {item["client"]: item["client_version"]},
            "status": "passed",
            "reason": "contract fixture metadata validated",
            "evidence_files": [path],
            "evidence_sha256": _evidence_hashes([path]),
        })
    return cases


def _not_run_live_cases(clients: dict[str, Any]) -> list[dict[str, Any]]:
    definitions = (
        (case_id, capability, [name for name in CLIENTS if name in required_clients])
        for case_id, (capability, required_clients) in LIVE_CASE_REQUIREMENTS.items()
    )
    return [{
        "id": case_id,
        "capability": capability,
        "proof_level": "live",
        "clients": names,
        "client_versions": {name: clients[name]["version"] for name in names},
        "status": "not-run",
        "reason": "no live evidence was supplied; offline verification never starts a model",
        "evidence_files": [],
        "evidence_sha256": {},
    } for case_id, capability, names in definitions]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_live_evidence(
    path: Path, expected_build: dict[str, Any], expected_environment: dict[str, str]
) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ReceiptError("live evidence must be a JSON object")
    if document.get("schema") != LIVE_SCHEMA or not isinstance(document.get("cases"), list):
        raise ReceiptError("unsupported live evidence schema")
    build_binding = document.get("build")
    expected_build_binding = {
        key: expected_build[key] for key in ("package_version", "content_sha256")
    }
    if build_binding != expected_build_binding:
        raise ReceiptError("live evidence belongs to a different build")
    environment_binding = document.get("environment")
    if environment_binding != expected_environment:
        raise ReceiptError("live evidence belongs to a different platform")
    root = path.resolve().parent
    cases = []
    seen_ids: set[str] = set()
    for raw in document["cases"]:
        if not isinstance(raw, dict):
            raise ReceiptError("each live case must be a JSON object")
        case = dict(raw)
        for field in ("id", "capability", "clients", "client_versions", "status", "reason", "evidence_files"):
            if field not in case:
                raise ReceiptError(f"live case missing {field}")
        if not isinstance(case["id"], str) or not case["id"] or case["id"] in seen_ids:
            raise ReceiptError("live case ids must be non-empty and unique")
        seen_ids.add(case["id"])
        if not isinstance(case["capability"], str) or not case["capability"]:
            raise ReceiptError(f"{case['id']}: capability must be a non-empty string")
        if (
            not isinstance(case["status"], str)
            or case["status"] not in STATUSES
            or not isinstance(case["reason"], str)
            or not case["reason"]
        ):
            raise ReceiptError(f"{case['id']}: invalid status or empty reason")
        if (
            not isinstance(case["clients"], list)
            or not case["clients"]
            or any(not isinstance(client, str) or client not in CLIENTS for client in case["clients"])
            or len(set(case["clients"])) != len(case["clients"])
        ):
            raise ReceiptError(f"{case['id']}: clients are required")
        if case["capability"] == "authentication" and len(case["clients"]) != 1:
            raise ReceiptError(f"{case['id']}: authentication evidence requires one client")
        if (
            not isinstance(case["client_versions"], dict)
            or set(case["client_versions"]) != set(case["clients"])
            or any(not isinstance(version, str) or not version for version in case["client_versions"].values())
        ):
            raise ReceiptError(f"{case['id']}: client_versions must match clients")
        if not isinstance(case["evidence_files"], list) or any(
            not isinstance(item, str) or not item for item in case["evidence_files"]
        ):
            raise ReceiptError(f"{case['id']}: evidence_files must be a list of paths")
        evidence: list[str] = []
        for relative in case["evidence_files"]:
            relative_path = Path(relative)
            resolved = (root / relative_path).resolve()
            if relative_path.is_absolute() or not _inside(resolved, root):
                raise ReceiptError(f"{case['id']}: evidence path escapes evidence directory")
            if not resolved.is_file():
                raise ReceiptError(f"{case['id']}: evidence file does not exist: {relative}")
            evidence.append(str(resolved))
        if case["status"] != "not-run" and not evidence:
            raise ReceiptError(f"{case['id']}: executed case requires evidence")
        case["proof_level"] = "live"
        case["evidence_files"] = evidence
        case["evidence_sha256"] = _evidence_hashes(evidence)
        case["verified_against"] = {
            "build": build_binding,
            "environment": environment_binding,
        }
        cases.append(case)
    return cases


def _replace_placeholder(cases: list[dict[str, Any]], imported: list[dict[str, Any]]) -> list[dict[str, Any]]:
    imported_ids = {case["id"] for case in imported}
    kept = [
        case for case in cases
        if not (case["proof_level"] == "live" and case["id"] in imported_ids)
    ]
    return kept + imported


def _apply_authentication_evidence(
    clients: dict[str, Any], imported: list[dict[str, Any]]
) -> None:
    for case in imported:
        if case["capability"] != "authentication":
            continue
        client = case["clients"][0]
        clients[client]["authenticated"] = {
            "status": case["status"],
            "reason": case["reason"],
            "client_version": case["client_versions"][client],
            "evidence_files": case["evidence_files"],
            "evidence_sha256": case["evidence_sha256"],
        }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def make_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = args.root.resolve()
    output = args.output.resolve()
    if output == root or root in output.parents:
        raise ReceiptError("compatibility output must be outside the package root")
    env, lab = _lab_environment(output.parent)
    clients = {name: _client_details(name, env) for name in CLIENTS}
    build = build_identity(root)
    environment = {
        "python": platform.python_version(), "system": platform.system(),
        "release": platform.release(), "machine": platform.machine(),
    }
    failures = False
    try:
        fixtures = load_fixtures(args.fixtures.resolve())
        cases = _fixture_cases(fixtures) + _not_run_live_cases(clients)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        failures = True
        cases = [{
            "id": "contract-fixture-validation", "capability": "fixtures",
            "proof_level": "deterministic", "clients": [], "client_versions": {},
            "status": "failed", "reason": str(error), "evidence_files": [],
            "evidence_sha256": {},
        }] + _not_run_live_cases(clients)
    if args.live_evidence:
        try:
            binding_environment = {
                key: environment[key] for key in ("system", "release", "machine")
            }
            imported = load_live_evidence(args.live_evidence.resolve(), build, binding_environment)
            cases = _replace_placeholder(
                cases,
                imported,
            )
            _apply_authentication_evidence(clients, imported)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures = True
            cases.append({
                "id": "live-evidence-import", "capability": "evidence-import",
                "proof_level": "deterministic", "clients": [], "client_versions": {},
                "status": "failed", "reason": str(error), "evidence_files": [],
                "evidence_sha256": {},
            })
    summary = {status.replace("-", "_"): sum(case["status"] == status for case in cases) for status in STATUSES}
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "build": build,
        "environment": environment,
        "lab": lab,
        "clients": clients,
        "cases": cases,
        "summary": summary,
    }
    return report, 1 if failures or summary["failed"] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--root", "--package-root", dest="root", type=Path, default=default_root)
    parser.add_argument("--fixtures", type=Path, default=default_root / "tests/compat/fixtures")
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "session-handoff/compatibility-report.json")
    parser.add_argument("--live-evidence", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report, status = make_report(args)
    _write_json(args.output.resolve(), redact_value(report))
    print(args.output.resolve())
    return status


if __name__ == "__main__":
    raise SystemExit(main())
