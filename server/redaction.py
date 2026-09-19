"""Shared secret redaction used at every persistence boundary."""
from __future__ import annotations

import re
from typing import Any

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
_SECRET_KEY_RE = re.compile(_SECRET_KEY, re.IGNORECASE)
_YAML_BLOCK = re.compile(
    rf"^(?P<prefix>[ ]*(?:-[ ]+)?)(?P<keyquote>['\"]?)(?P<key>{_SECRET_KEY})(?P=keyquote)"
    r"(?P<spacing>[ ]*):(?P<after>[ ]*)"
    r"(?P<indicator>[|>](?:[1-9][+-]?|[+-]?[1-9]|[+-]?))"
    r"(?P<comment>[ ]+#.*)?[ ]*$",
    re.IGNORECASE,
)


def is_secret_key(key: object) -> bool:
    return isinstance(key, str) and _SECRET_KEY_RE.fullmatch(key) is not None


def redact_value(value: Any) -> Any:
    """Recursively redact sensitive mapping values before serialization."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if is_secret_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_secrets(value)[0]
    return value


def _redact_yaml_blocks(text: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    count = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = _YAML_BLOCK.match(body)
        if match is None:
            output.append(line)
            index += 1
            continue
        replacement = (
            f"{match.group('prefix')}{match.group('keyquote')}{match.group('key')}"
            f"{match.group('keyquote')}{match.group('spacing')}:"
            f"{match.group('after')}[REDACTED]"
        )
        if match.group("comment"):
            replacement += match.group("comment")
        output.append(replacement + ending)
        count += 1
        base_indent = len(match.group("prefix").expandtabs(8))
        index += 1
        while index < len(lines):
            candidate = lines[index].rstrip("\r\n")
            if not candidate.strip():
                lookahead = index + 1
                while lookahead < len(lines) and not lines[lookahead].rstrip("\r\n").strip():
                    lookahead += 1
                if lookahead < len(lines):
                    next_body = lines[lookahead].rstrip("\r\n")
                    next_indent = len(next_body) - len(next_body.lstrip(" \t"))
                    if next_indent > base_indent:
                        index += 1
                        continue
                break
            candidate_indent = len(candidate) - len(candidate.lstrip(" \t"))
            if candidate_indent > base_indent:
                index += 1
                continue
            break
    return "".join(output), count


def redact_secrets(text: str) -> tuple[str, int]:
    """Redact common credential forms without changing existing markers."""
    text, count = _redact_yaml_blocks(text)

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
