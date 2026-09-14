"""Shared secret redaction used at every persistence boundary."""
from __future__ import annotations

import re

_SECRET_WORD = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION)"
_SECRET_KEY = rf"\b(?:{_SECRET_WORD}|[A-Za-z][A-Za-z0-9_-]*{_SECRET_WORD})\b"
_ASSIGNMENT = re.compile(
    r"(?P<keyquote>['\"]?)(?P<key>"
    + _SECRET_KEY
    + r")(?P=keyquote)(?P<spacing>\s*)(?P<separator>[:=])(?P<after>\s*)"
    r"(?:"
    r"(?P<single>'(?P<single_value>(?:\\[\s\S]|[^'\\])*)')"
    r"|(?P<double>\"(?P<double_value>(?:\\[\s\S]|[^\"\\])*)\")"
    r"|(?P<malformed_single_quote>'(?P<malformed_single>(?:\\[\s\S]|[^\\])*\\?)\Z)"
    r"|(?P<malformed_double_quote>\"(?P<malformed_double>(?:\\[\s\S]|[^\\])*\\?)\Z)"
    r"|(?P<unquoted>(?:(?:Basic|Bearer)\s+)?(?:"
    r"\[REDACTED\](?:;[^\s'\",;\)\]}]+)?"
    r"|[^\s'\",;\)\]}]+(?:;[^\s'\",;\)\]}]+)*"
    r"))"
    r")",
    re.IGNORECASE,
)
_MALFORMED = re.compile(
    rf"(?P<prefix>(?P<keyquote>['\"]?){_SECRET_KEY}(?P=keyquote)\s*[:=]\s*(?P<quote>['\"]?)\[REDACTED\](?P=quote))"
    r"(?P<attached>(?:(?:Basic|Bearer)\s+[^\s'\"`,;\)\]}]+(?:;[^\s'\"`,;\)\]}]+)*|;[^\s'\"`,;\)\]}]+|[^\s'\"`,;\)\]}]+))", re.IGNORECASE,
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

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        if match.group("single") is not None:
            quoted = "'"
        elif match.group("double") is not None:
            quoted = '"'
        else:
            quoted = None
        unquoted = match.group("unquoted")
        malformed = match.group("malformed_single")
        if malformed is None:
            malformed = match.group("malformed_double")
        if malformed is not None:
            if malformed in {"[REDACTED]", "Basic [REDACTED]", "Bearer [REDACTED]"}:
                return match.group(0)
            count += 1
            quote = "'" if match.group("malformed_single_quote") is not None else '"'
            return (
                f"{match.group('keyquote')}{match.group('key')}{match.group('keyquote')}"
                f"{match.group('spacing')}{match.group('separator')}{match.group('after')}"
                f"{quote}[REDACTED]"
            )
        value = match.group("single_value") or match.group("double_value") or unquoted
        if value in {"[REDACTED]", "Basic [REDACTED]", "Bearer [REDACTED]"}:
            return match.group(0)
        count += 1
        marker = "[REDACTED]"
        replacement = (
            f"{quoted}{marker}{quoted}"
            if quoted is not None else marker
        )
        return (
            f"{match.group('keyquote')}{match.group('key')}{match.group('keyquote')}"
            f"{match.group('spacing')}{match.group('separator')}{match.group('after')}"
            f"{replacement}"
        )

    text = _ASSIGNMENT.sub(assignment, text)

    def malformed(match: re.Match[str]) -> str:
        nonlocal count
        if not match.group("attached"):
            return match.group("prefix")
        count += 1
        return match.group("prefix")

    text = _MALFORMED.sub(malformed, text)
    text = substitute(_BEARER, "Bearer [REDACTED]", text)
    text = substitute(_TOKEN, "[REDACTED]", text)
    return text, count
