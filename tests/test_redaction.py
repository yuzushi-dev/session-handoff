import json
import subprocess
import sys

import pytest

from server.redaction import redact_secrets


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("PASSWORD=plain-secret", "PASSWORD=[REDACTED]"),
        ("TOKEN: plain-secret", "TOKEN: [REDACTED]"),
        ('"AUTHORIZATION": "value with spaces; punctuation, too"', '"AUTHORIZATION": "[REDACTED]"'),
        ("TOKEN=[REDACTED]leftover", "TOKEN=[REDACTED]"),
        ("TOKEN='[REDACTED]'leftover", "TOKEN='[REDACTED]'"),
        ('"TOKEN": "[REDACTED]"leftover', '"TOKEN": "[REDACTED]"'),
        ('{"source_path": "API_TOKEN=secret"}', '{"source_path": "API_TOKEN=[REDACTED]"}'),
    ],
)
def test_redact_assignment_keys_and_quoted_values(source, expected):
    redacted, count = redact_secrets(source)

    assert redacted == expected
    assert count == 1


def test_redact_existing_marker_is_idempotent():
    source = '"PASSWORD": "[REDACTED]"'

    assert redact_secrets(source) == (source, 0)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("API_TOKEN=abc;def", "API_TOKEN=[REDACTED]"),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: [REDACTED]"),
        ("API_TOKEN=[REDACTED];secret-tail", "API_TOKEN=[REDACTED]"),
        ("TOKEN=`synthetic-test-value`", "TOKEN=[REDACTED]"),
        ("TOKEN=[synthetic-test-value]", "TOKEN=[REDACTED]]"),
        ('API_TOKEN="[REDACTED]";secret-tail', 'API_TOKEN="[REDACTED]"'),
        ('"AUTHORIZATION": "Basic dXNlcjpwYXNz"', '"AUTHORIZATION": "[REDACTED]"'),
        ('API_TOKEN=abc;def, "next":"keep"', 'API_TOKEN=[REDACTED], "next":"keep"'),
    ],
)
def test_redact_delimited_credentials_without_swallowing_following_fields(source, expected):
    redacted, count = redact_secrets(source)

    assert redacted == expected
    assert count == 1


def test_redacted_authorization_scheme_is_idempotent():
    assert redact_secrets("Authorization: Basic [REDACTED]") == (
        "Authorization: Basic [REDACTED]",
        0,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r'PASSWORD="value \"with\" \\slashes"', 'PASSWORD="[REDACTED]"'),
        (r"TOKEN='value \'with\' \\slashes'", "TOKEN='[REDACTED]'"),
        ('PASSWORD="pässw🚫rd"', 'PASSWORD="[REDACTED]"'),
    ],
)
def test_redact_quoted_escapes_and_unicode(source, expected):
    redacted, count = redact_secrets(source)

    assert redacted == expected
    assert count == 1


def test_redact_unterminated_quoted_assignment():
    source = 'TOKEN="unterminated secret'

    assert redact_secrets(source) == ('TOKEN="[REDACTED]', 1)


def test_redact_unterminated_quoted_backslashes_in_isolated_process():
    source = 'TOKEN="' + "\\" * 40 + "secret"
    code = (
        "import json, sys; "
        "from server.redaction import redact_secrets; "
        "print(json.dumps(redact_secrets(sys.argv[1])))"
    )

    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, source],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"redaction timed out: {exc}")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ['TOKEN="[REDACTED]', 1]
