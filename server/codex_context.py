"""Parse request-scoped Codex metadata hints, never cross-thread authority."""

from dataclasses import dataclass
import unicodedata


MAX_ID_LENGTH = 256


@dataclass(frozen=True)
class CodexContext:
    thread_id: str | None = None
    session_id: str | None = None


def _validated_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID_LENGTH:
        return None
    if any(unicodedata.category(char) == "Cc" for char in value):
        return None
    return value


def parse_request_context(params: object) -> CodexContext:
    """Read direct params._meta IDs without fallback, mutation, or global binding."""
    if not isinstance(params, dict):
        return CodexContext()
    metadata = params.get("_meta")
    if not isinstance(metadata, dict):
        return CodexContext()
    return CodexContext(
        thread_id=_validated_id(metadata.get("threadId")),
        session_id=_validated_id(metadata.get("sessionId")),
    )
