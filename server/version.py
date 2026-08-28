"""The installed package version, loaded once from the package metadata."""

from functools import lru_cache
import json
import re
from pathlib import Path


VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
_PACKAGE_JSON = Path(__file__).resolve().parents[1] / "package.json"


@lru_cache(maxsize=1)
def package_version() -> str:
    try:
        with _PACKAGE_JSON.open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        version = metadata["version"]
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise RuntimeError(f"cannot load package version from {_PACKAGE_JSON}") from exc
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise RuntimeError(f"invalid package version in {_PACKAGE_JSON}")
    return version


PACKAGE_VERSION = package_version()
