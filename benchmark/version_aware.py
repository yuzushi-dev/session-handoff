"""Deterministic, build-aware comparison for session-handoff releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


MANIFESTS = (
    "package.json",
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
)
SCENARIOS = (
    "manifest_alignment",
    "fresh_session_start",
    "session_start_replay",
    "consent_yes",
    "consent_no",
    "legacy_config",
    "do_not_track",
)
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
ISO_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z"
)
DYNAMIC_KEYS = frozenset({"at", "consented_at", "recorded_at"})
SNAPSHOT_LIMIT = 256 * 1024


class BuildSpecError(ValueError):
    """A build spec or build tree is not usable by the benchmark."""


@dataclass(frozen=True)
class Build:
    label: str
    root: Path
    identity: dict[str, Any]


def parse_build_spec(value: str) -> tuple[str, Path]:
    if not isinstance(value, str):
        raise BuildSpecError("build spec must be LABEL=ROOT")
    label, separator, raw_root = value.partition("=")
    label = label.strip()
    raw_root = raw_root.strip()
    if not separator or not label or not raw_root:
        raise BuildSpecError("build spec must be LABEL=ROOT")
    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        raise BuildSpecError(f"build root is not a directory: {raw_root}")
    return label, root


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildSpecError(f"invalid JSON manifest: {path}") from exc
    if not isinstance(value, dict):
        raise BuildSpecError(f"manifest must be an object: {path}")
    return value


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if listed.returncode == 0:
        paths = (
            root / os.fsdecode(relative)
            for relative in listed.stdout.split(b"\0")
            if relative
        )
    else:
        paths = (
            path
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts
        )
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _git_revision(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def load_build(label: str, root: Path) -> Build:
    if not isinstance(label, str) or not label.strip():
        raise BuildSpecError("build label must not be empty")
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise BuildSpecError(f"build root is not a directory: {root}")

    versions: dict[str, str] = {}
    for relative in MANIFESTS:
        path = root / relative
        if not path.is_file():
            raise BuildSpecError(f"missing manifest: {path}")
        manifest = _read_json(path)
        version = manifest.get("version")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise BuildSpecError(f"invalid manifest version: {path}")
        versions[relative] = version
    if len(set(versions.values())) != 1:
        raise BuildSpecError(f"manifest versions disagree in {root}")

    identity = {
        "label": label,
        "root": str(root),
        "package_version": versions["package.json"],
        "manifest_versions": versions,
        "git_revision": _git_revision(root),
        "tree_sha256": _tree_sha256(root),
    }
    return Build(label=label, root=root, identity=identity)


def _normalise_value(value: Any, root: Path, home: Path, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            field: _normalise_value(item, root, home, field)
            for field, item in value.items()
        }
    if isinstance(value, list):
        return [_normalise_value(item, root, home) for item in value]
    if not isinstance(value, str):
        return value
    if key in DYNAMIC_KEYS:
        return "<timestamp>"
    return ISO_TIMESTAMP_RE.sub(
        "<timestamp>", value.replace(str(home), "<home>").replace(str(root), "<build>")
    )


def _normalise_text(value: str, root: Path, home: Path) -> str:
    value = value or ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ISO_TIMESTAMP_RE.sub(
            "<timestamp>", value.replace(str(home), "<home>").replace(str(root), "<build>")
        ).strip()
    return json.dumps(
        _normalise_value(parsed, root, home), sort_keys=True, separators=(",", ":")
    )


def _minimal_environment(home: Path, *, do_not_track: bool) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
        if name in os.environ
    }
    environment.update(
        {
            "HOME": str(home),
            "SESSION_HANDOFF_HOME": str(home),
            "DO_NOT_TRACK": "1" if do_not_track else "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "TZ": "UTC",
        }
    )
    return environment


def run_product_command(
    build: Build,
    relative_command: str,
    *arguments: str,
    home: Path,
    input_text: str = "",
    do_not_track: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    command = (build.root / relative_command).resolve()
    try:
        command.relative_to(build.root)
    except ValueError as exc:
        raise BuildSpecError(f"command escapes build root: {relative_command}") from exc
    if not command.is_file():
        return {
            "supported": False,
            "returncode": None,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "reason": "missing_command",
        }

    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [sys.executable, str(command), *arguments],
            cwd=build.root,
            env=_minimal_environment(home, do_not_track=do_not_track),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "supported": True,
            "returncode": None,
            "timed_out": True,
            "stdout": _normalise_text(stdout, build.root, home),
            "stderr": _normalise_text(stderr, build.root, home),
        }
    return {
        "supported": True,
        "returncode": result.returncode,
        "timed_out": False,
        "stdout": _normalise_text(result.stdout, build.root, home),
        "stderr": _normalise_text(result.stderr, build.root, home),
    }


def _step(result: dict[str, Any], expected: Sequence[int] = (0,), *, allow_unsupported=False) -> dict[str, Any]:
    result = dict(result)
    result["ok"] = (
        (not result["supported"] and allow_unsupported)
        or (
            result["supported"]
            and not result["timed_out"]
            and result["returncode"] in expected
        )
    )
    return result


def _json_stdout(result: Mapping[str, Any]) -> Any:
    try:
        return json.loads(result["stdout"])
    except (TypeError, json.JSONDecodeError):
        return {"parse_error": True, "stdout": result.get("stdout", "")}


def _consent_state(home: Path) -> str:
    path = home / ".config/session-handoff/telemetry.json"
    if not path.is_file():
        return "unasked"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid"
    if isinstance(config, dict) and isinstance(config.get("consent_state"), str):
        return config["consent_state"]
    if isinstance(config, dict) and config.get("enabled") is True:
        return "enabled"
    if isinstance(config, dict) and "prompted_consent_version" in config:
        return "legacy_asked"
    return "invalid"


def _snapshot_home(home: Path, root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(
        item
        for item in home.rglob("*")
        if item.is_file()
        and not item.is_symlink()
        and item.name != "telemetry-generation"
        and not item.name.endswith(".lock")
    ):
        relative = path.relative_to(home).as_posix()
        content = path.read_bytes()
        entry: dict[str, Any] = {"path": relative, "bytes": len(content)}
        if len(content) <= SNAPSHOT_LIMIT:
            try:
                if path.suffix == ".json":
                    entry["json"] = _normalise_value(
                        json.loads(content.decode("utf-8")), root, home
                    )
                elif path.suffix == ".jsonl":
                    entry["jsonl"] = [
                        _normalise_value(json.loads(line), root, home)
                        for line in content.decode("utf-8").splitlines()
                        if line.strip()
                    ]
                else:
                    entry["sha256"] = hashlib.sha256(content).hexdigest()
            except (UnicodeDecodeError, json.JSONDecodeError):
                entry["sha256"] = hashlib.sha256(content).hexdigest()
        else:
            entry["sha256"] = hashlib.sha256(content).hexdigest()
        files.append(entry)
    return files


def _scenario(build: Build, scenario: str) -> dict[str, Any]:
    if scenario == "manifest_alignment":
        versions = build.identity["manifest_versions"]
        return {
            "valid": len(set(versions.values())) == 1,
            "observation": {
                "capability": "supported",
                "package_version": build.identity["package_version"],
                "manifest_versions": versions,
            },
        }

    with tempfile.TemporaryDirectory(prefix="session-handoff-version-") as temporary:
        home = Path(temporary)
        if scenario == "fresh_session_start":
            hook = _step(run_product_command(build, "hooks/session-start.py", home=home))
            observation = {
                "capability": "supported" if hook["supported"] else "unsupported",
                "hook": _json_stdout(hook),
                "consent_state": _consent_state(home),
                "files": _snapshot_home(home, build.root),
            }
            return {"valid": hook["ok"], "observation": observation}

        if scenario == "session_start_replay":
            first = _step(run_product_command(build, "hooks/session-start.py", home=home))
            second = _step(run_product_command(build, "hooks/session-start.py", home=home))
            observation = {
                "capability": "supported" if first["supported"] and second["supported"] else "unsupported",
                "first": _json_stdout(first),
                "second": _json_stdout(second),
                "consent_state": _consent_state(home),
                "files": _snapshot_home(home, build.root),
            }
            return {"valid": first["ok"] and second["ok"], "observation": observation}

        if scenario in {"consent_yes", "consent_no"}:
            answer = "yes" if scenario.endswith("yes") else "no"
            first = _step(run_product_command(build, "hooks/session-start.py", home=home))
            response = _step(
                run_product_command(
                    build,
                    "hooks/user-prompt-submit.py",
                    home=home,
                    input_text=json.dumps({"prompt": f"session-handoff telemetry {answer}"}),
                ),
                allow_unsupported=True,
            )
            status = _step(
                run_product_command(build, "bin/session-handoff", "telemetry", "status", home=home)
            )
            observation = {
                "capability": "supported" if response["supported"] else "unsupported",
                "first": _json_stdout(first),
                "response": response,
                "status": status["stdout"],
                "consent_state": _consent_state(home),
                "files": _snapshot_home(home, build.root),
            }
            return {
                "valid": first["ok"] and response["ok"] and status["ok"],
                "observation": observation,
            }

        if scenario == "legacy_config":
            config_path = home / ".config/session-handoff/telemetry.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {"schema_version": 1, "enabled": False, "prompted_consent_version": 1}
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            enable = _step(
                run_product_command(build, "bin/session-handoff", "telemetry", "enable", home=home),
                expected=(0, 2),
            )
            hook = _step(run_product_command(build, "hooks/session-start.py", home=home))
            observation = {
                "capability": "supported",
                "enable": enable,
                "hook": _json_stdout(hook),
                "status": _step(
                    run_product_command(build, "bin/session-handoff", "telemetry", "status", home=home)
                )["stdout"],
                "consent_state": _consent_state(home),
                "files": _snapshot_home(home, build.root),
            }
            return {"valid": enable["ok"] and hook["ok"], "observation": observation}

        if scenario == "do_not_track":
            hook = _step(
                run_product_command(
                    build, "hooks/session-start.py", home=home, do_not_track=True
                )
            )
            status = _step(
                run_product_command(
                    build,
                    "bin/session-handoff",
                    "telemetry",
                    "status",
                    home=home,
                    do_not_track=True,
                )
            )
            observation = {
                "capability": "supported" if hook["supported"] and status["supported"] else "unsupported",
                "hook": _json_stdout(hook),
                "status": status["stdout"],
                "consent_state": _consent_state(home),
                "files": _snapshot_home(home, build.root),
            }
            return {"valid": hook["ok"] and status["ok"], "observation": observation}

    raise BuildSpecError(f"unknown scenario: {scenario}")


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        if not value:
            return {path or "/": {}}
        flattened: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{path}/{key}" if path else f"/{key}"
            flattened.update(_flatten(value[key], child))
        return flattened
    if isinstance(value, list):
        if not value:
            return {path or "/": []}
        flattened = {}
        for index, item in enumerate(value):
            flattened.update(_flatten(item, f"{path}/{index}"))
        return flattened
    return {path or "/": value}


def compare_observations(observations: Mapping[str, Any]) -> dict[str, Any]:
    if not observations:
        raise ValueError("at least one observation is required")
    flattened = {label: _flatten(value) for label, value in observations.items()}
    paths = sorted({path for value in flattened.values() for path in value})
    differences = []
    for path in paths:
        values = {
            label: flattened[label].get(path, "<missing>") for label in observations
        }
        if len({json.dumps(value, sort_keys=True) for value in values.values()}) > 1:
            differences.append({"path": path, "values": values})
    unsupported = [
        label
        for label, value in observations.items()
        if isinstance(value, dict) and value.get("capability") == "unsupported"
    ]
    status = "unsupported" if unsupported else ("different" if differences else "same")
    result: dict[str, Any] = {"status": status, "differences": differences}
    if unsupported:
        result["unsupported_builds"] = unsupported
    return result


def run_benchmark(builds: Sequence[Build], output: Path | None = None) -> dict[str, Any]:
    if len(builds) != 2:
        raise BuildSpecError("exactly two builds are required")
    labels = [build.label for build in builds]
    if len(set(labels)) != len(labels):
        raise BuildSpecError("build labels must be unique")

    results = {build.label: {} for build in builds}
    valid = True
    for build in builds:
        for scenario in SCENARIOS:
            result = _scenario(build, scenario)
            results[build.label][scenario] = result
            valid = valid and result["valid"]

    comparisons: dict[str, Any] = {}
    for scenario in SCENARIOS:
        observations = {
            label: results[label][scenario]["observation"] for label in labels
        }
        comparison = compare_observations(observations)
        if not all(results[label][scenario]["valid"] for label in labels):
            comparison["status"] = "error"
        comparisons[scenario] = comparison

    payload = {
        "schema_version": 1,
        "benchmark": {
            "name": "session-handoff-version-aware",
            "version": 1,
            "network": "not_used",
            "provider_calls": 0,
            "scenarios": list(SCENARIOS),
        },
        "builds": [build.identity for build in builds],
        "results": results,
        "comparisons": comparisons,
        "valid": valid,
    }
    if output is not None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build",
        action="append",
        required=True,
        metavar="LABEL=ROOT",
        help="repeat exactly twice",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        builds = [load_build(*parse_build_spec(value)) for value in args.build]
        payload = run_benchmark(builds, args.output)
    except BuildSpecError as exc:
        print(f"version-aware benchmark: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(args.output),
                "valid": payload["valid"],
                "provider_calls": payload["benchmark"]["provider_calls"],
                "comparison_status": {
                    scenario: value["status"]
                    for scenario, value in payload["comparisons"].items()
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if payload["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
