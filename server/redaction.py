"""Shared secret redaction used at every persistence boundary."""
from __future__ import annotations

import re

_SECRET_KEY = r"\b[A-Za-z][A-Za-z0-9_-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION)\b"
_ASSIGNMENT = re.compile(
    rf"(?P<key>{_SECRET_KEY})(?P<spacing>\s*)(?P<separator>[:=])(?P<after>\s*)"
    r"(?P<quote>['\"]?)(?P<value>[^\s'\"`;,\)\]]+)(?P=quote)", re.IGNORECASE,
)
_MALFORMED = re.compile(
    rf"(?P<prefix>{_SECRET_KEY}\s*[:=]\s*(?P<quote>['\"]?)\[REDACTED\](?P=quote))"
    r"(?P<attached>[^\s'\"`;)\],]+)", re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
)
_PRIVATE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL,
)


def redact_secrets(text: str) -> tuple[str, int]:
    """Redact common credential forms without changing existing markers."""
    count = 0

    def substitute(pattern: re.Pattern[str], replacement, value: str) -> str:
        nonlocal count

        def apply(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return replacement(match) if callable(replacement) else replacement

        return pattern.sub(apply, value)

    text = substitute(_PRIVATE, "[PRIVATE KEY REDACTED]", text)
    text = substitute(_BEARER, "Bearer [REDACTED]", text)
    text = substitute(_TOKEN, "[REDACTED]", text)
    text = substitute(_MALFORMED, lambda match: match.group("prefix"), text)

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        if match.group("value") == "[REDACTED" and match.end() < len(match.string) and match.string[match.end()] == "]":
            return match.group(0)
        count += 1
        return (
            f"{match.group('key')}{match.group('spacing')}{match.group('separator')}"
            f"{match.group('after')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        )

    return _ASSIGNMENT.sub(assignment, text), count
