"""Shared build identity helpers for compatibility receipts and doctor."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.+-]+)?)")
COMPATIBILITY_CLIENTS = ("codex", "claude")
LIVE_CASE_REQUIREMENTS = {
    "native-installation": ("installation", frozenset(COMPATIBILITY_CLIENTS)),
    "claude-handoff": ("handoff", frozenset(("claude",))),
    "codex-handoff": ("handoff", frozenset(("codex",))),
    "claude-to-codex-migration": ("migration", frozenset(COMPATIBILITY_CLIENTS)),
    "codex-to-claude-migration": ("migration", frozenset(COMPATIBILITY_CLIENTS)),
    "manual-compaction": ("compaction", frozenset(COMPATIBILITY_CLIENTS)),
    "automatic-compaction": ("compaction", frozenset(COMPATIBILITY_CLIENTS)),
}


def normalize_version(output: str) -> str | None:
    """Return a comparable client version from a human-readable banner."""
    match = VERSION_RE.search(output)
    return match.group(1) if match else None


def package_files(root: Path, package: dict[str, Any]) -> list[Path]:
    paths = {root / "package.json"}
    for entry in package.get("files", []):
        candidate = root / entry
        if any(char in entry for char in "*?["):
            paths.update(path for path in root.glob(entry) if path.is_file())
        elif candidate.is_dir():
            paths.update(path for path in candidate.rglob("*") if path.is_file())
        elif candidate.is_file():
            paths.add(candidate)
    return sorted(paths)


def content_hash(root: Path) -> str:
    """Hash the effective files selected by package.json's files list."""
    root = root.resolve()
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    for path in package_files(root, package):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
