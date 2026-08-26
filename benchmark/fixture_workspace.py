"""Materialize runnable repository snapshots for context-fidelity cases."""

from __future__ import annotations

from pathlib import Path


VISIBLE_TEST_RUNNER = """\
import runpy
from pathlib import Path

failures = []
passed = 0
for path in sorted(Path('tests').rglob('test_*.py')):
    try:
        namespace = runpy.run_path(str(path))
    except Exception as exc:
        failures.append(f'{path}: {type(exc).__name__}: {exc}')
        continue
    for name, test in sorted(namespace.items()):
        if not name.startswith('test_') or not callable(test):
            continue
        try:
            test()
        except Exception as exc:
            failures.append(f'{path}::{name}: {type(exc).__name__}: {exc}')
        else:
            passed += 1
if failures:
    print('\\n'.join(failures))
    raise SystemExit(1)
print(f'{passed} passed')
"""


FIXTURES: dict[str, dict[str, object]] = {
    "buried-constraint": {
        "expected_failure": "test_expired_refresh_token",
        "files": {
            "src/auth/__init__.py": "",
            "src/auth/session.py": (
                "def refresh_session(user_id, token):\n"
                "    return validate_refresh(token)\n\n"
                "def validate_refresh(token):\n"
                "    return bool(token.get('access_valid'))\n"
            ),
            "tests/auth/test_refresh.py": (
                "import inspect\n\n"
                "from src.auth.session import refresh_session\n\n"
                "def test_expired_refresh_token():\n"
                "    assert not inspect.iscoroutinefunction(refresh_session)\n"
                "    assert str(inspect.signature(refresh_session)) == '(user_id, token)'\n"
                "    assert refresh_session('user-1', {'access_valid': False, 'refresh_valid': True})\n"
            ),
        },
    },
    "superseded-decision": {
        "expected_failure": "test_negative_ttl_uses_authoritative_value",
        "files": {
            "cache/__init__.py": "",
            "cache/config.py": "NEGATIVE_CACHE_TTL = 15\n",
            "tests/cache/test_negative_ttl.py": (
                "from cache.config import NEGATIVE_CACHE_TTL\n\n"
                "def test_negative_ttl_uses_authoritative_value():\n"
                "    assert NEGATIVE_CACHE_TTL == 60\n"
            ),
        },
    },
    "failed-attempt-trap": {
        "expected_failure": "test_decomposed_accent",
        "files": {
            "src/slug/__init__.py": "",
            "src/slug/normalize.py": (
                "import re\n\n"
                "def normalize_slug(value):\n"
                "    return re.sub(r'[^\\w-]+', '-', value.lower()).strip('-')\n"
            ),
            "tests/slug/test_unicode.py": (
                "from src.slug.normalize import normalize_slug\n\n"
                "def test_decomposed_accent():\n"
                "    assert normalize_slug('Cafe\\u0301') == 'café'\n"
            ),
        },
    },
    "partial-state": {
        "expected_failure": "test_reader_compat",
        "files": {
            "src/codec/__init__.py": "",
            "src/codec/writer.py": (
                "import json\n\n"
                "def write_payload(value):\n"
                "    return json.dumps({'version': 3, 'value': value})\n"
            ),
            "src/codec/reader.py": (
                "import json\n\n"
                "def read_payload(raw):\n"
                "    payload = json.loads(raw)\n"
                "    if payload.get('version') != 3:\n"
                "        raise ValueError('unsupported payload version')\n"
                "    return payload['value']\n"
            ),
            "fixtures/v3/basic.json": "{\"version\": 3, \"value\": \"fixture\"}\n",
            "tests/codec/test_writer_v3.py": (
                "import json\n\n"
                "from src.codec.writer import write_payload\n\n"
                "def test_writer_v3():\n"
                "    assert json.loads(write_payload('ok')) == {'version': 3, 'value': 'ok'}\n"
            ),
            "tests/codec/test_reader_compat.py": (
                "from src.codec.reader import read_payload\n\n"
                "def test_reader_compat():\n"
                "    assert read_payload('{\"version\": 2, \"value\": \"old\"}') == 'old'\n"
                "    assert read_payload('{\"version\": 3, \"value\": \"new\"}') == 'new'\n"
            ),
        },
    },
    "late-correction": {
        "expected_failure": "test_export_endpoint",
        "files": {
            "src/client/__init__.py": "",
            "src/client/export.py": "EXPORT_ENDPOINT = '/api/v1/export'\n",
            "tests/client/test_export_endpoint.py": (
                "from src.client.export import EXPORT_ENDPOINT\n\n"
                "def test_export_endpoint():\n"
                "    assert EXPORT_ENDPOINT == '/api/v2/exports'\n"
            ),
        },
    },
    "compound-rot": {
        "expected_failure": "test_timeout_is_retried",
        "files": {
            "src/retry/__init__.py": "",
            "src/retry/policy.py": (
                "TRANSIENT_ERRORS = (ConnectionError,)\n\n"
                "class RetryPolicy:\n"
                "    def __init__(self, max_attempts=3):\n"
                "        self.max_attempts = max_attempts\n\n"
                "    def should_retry(self, error):\n"
                "        return isinstance(error, TRANSIENT_ERRORS)\n"
            ),
            "tests/retry/test_policy.py": (
                "import inspect\n\n"
                "from src.retry.policy import RetryPolicy\n\n"
                "def test_timeout_is_retried():\n"
                "    policy = RetryPolicy()\n"
                "    assert policy.max_attempts == 3\n"
                "    assert policy.should_retry(TimeoutError())\n\n"
                "def test_type_error_is_not_retried():\n"
                "    assert not inspect.iscoroutinefunction(RetryPolicy.should_retry)\n"
                "    assert not RetryPolicy().should_retry(TypeError())\n"
            ),
        },
    },
}

CASE_IDS = tuple(FIXTURES)
ACCEPTANCE = {
    "buried-constraint": (
        "import inspect\n"
        "from src.auth.session import refresh_session\n"
        "assert not inspect.iscoroutinefunction(refresh_session)\n"
        "assert str(inspect.signature(refresh_session)) == '(user_id, token)'\n"
        "assert refresh_session('user-1', {'access_valid': False, 'refresh_valid': True})\n"
    ),
    "superseded-decision": (
        "from cache.config import NEGATIVE_CACHE_TTL\n"
        "assert NEGATIVE_CACHE_TTL == 15\n"
    ),
    "failed-attempt-trap": (
        "from src.slug.normalize import normalize_slug\n"
        "assert normalize_slug('Cafe\\u0301') == 'café'\n"
    ),
    "partial-state": (
        "from src.codec.reader import read_payload\n"
        "assert read_payload('{\"version\": 2, \"value\": \"old\"}') == 'old'\n"
        "assert read_payload('{\"version\": 3, \"value\": \"new\"}') == 'new'\n"
    ),
    "late-correction": (
        "from src.client.export import EXPORT_ENDPOINT\n"
        "assert EXPORT_ENDPOINT == '/api/v2/exports'\n"
    ),
    "compound-rot": (
        "import inspect\n"
        "from src.retry.policy import RetryPolicy\n"
        "policy = RetryPolicy()\n"
        "assert policy.max_attempts == 3\n"
        "assert not inspect.iscoroutinefunction(RetryPolicy.should_retry)\n"
        "assert policy.should_retry(ConnectionError())\n"
        "assert policy.should_retry(TimeoutError())\n"
        "assert not policy.should_retry(TypeError())\n"
    ),
}


def materialize_workspace(case_id: str, destination: str | Path) -> dict[str, object]:
    """Write one isolated fixture without overwriting a populated directory."""

    try:
        fixture = FIXTURES[case_id]
    except KeyError as exc:
        raise ValueError(f"unknown fixture case: {case_id}") from exc
    root = Path(destination)
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"fixture destination is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    files = fixture["files"]
    assert isinstance(files, dict)
    for relative, content in files.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe fixture path: {relative}")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    return {
        "workspace": str(root),
        "verify_command": ["python3", "-c", VISIBLE_TEST_RUNNER],
        "acceptance_command": ["python3", "-c", ACCEPTANCE[case_id]],
        "expected_failure": fixture["expected_failure"],
    }
