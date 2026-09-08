from pathlib import Path

import pytest

from benchmark.product_roundtrip import ProductRoundtripError, product_identity, roundtrip


CONTENT = """## Goal
Ship it.
## Constraints & Preferences
- Keep it small.
## Progress
### Done
- Generated.
### In Progress
- None.
### Pending
- Continue.
## Key Decisions
- Use MCP.
## Critical Context
- Fixture.
## Next Steps
1. Continue.
"""


def _fake_product(root: Path) -> None:
    server = root / "server"
    server.mkdir(parents=True)
    (server / "handoff_mcp.py").write_text(
        '''import json, os, sys
request = json.loads(sys.stdin.readline())
args = request["params"]["arguments"]
assert args["auto_switch"] is False if request["params"]["name"] == "handoff_create" else True
assert os.environ["HOME"] != os.environ["XDG_DATA_HOME"]
cache = os.path.join(os.environ["XDG_STATE_HOME"], "content")
if request["params"]["name"] == "handoff_create":
    open(cache, "w").write(args["content"])
    data = {"path": "handoffs/pilot.md", "storage": "workspace"} if "path" in args else {"ref": "handoff://11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222", "storage": "central"}
else:
    data = {"content": open(cache).read(), "valid": True, "missing_sections": []}
print(json.dumps({"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":json.dumps(data)}]}}))
''',
        encoding="utf-8",
    )


@pytest.mark.parametrize("storage", ["legacy", "central"])
def test_roundtrip_uses_real_stdio_contract_and_returns_read_content(tmp_path, storage):
    root = tmp_path / "product"
    _fake_product(root)
    result = roundtrip(root, storage, tmp_path / "isolated", tmp_path / "workspace", CONTENT)

    assert result["content"] == CONTENT
    assert result["storage"] == storage
    assert result["content_sha256"]
    assert result["create_response_sha256"]
    assert result["read_response_sha256"]
    if storage == "legacy":
        assert result["create"]["path"] == "handoffs/pilot.md"
    else:
        assert result["create"]["ref"].startswith("handoff://")


def test_product_identity_changes_with_worktree_content(tmp_path):
    root = tmp_path / "product"
    _fake_product(root)
    first = product_identity(root)
    (root / "server/handoff_mcp.py").write_text("print('changed')\n", encoding="utf-8")

    assert first["root"] == str(root.resolve())
    assert first["tree_sha256"] != product_identity(root)["tree_sha256"]


def test_roundtrip_fails_closed_on_tool_error(tmp_path):
    root = tmp_path / "product"
    server = root / "server"
    server.mkdir(parents=True)
    (server / "handoff_mcp.py").write_text(
        'import json\nprint(json.dumps({"jsonrpc":"2.0","id":1,"result":{"isError":True,"content":[{"type":"text","text":"broken"}]}}))\n',
        encoding="utf-8",
    )

    with pytest.raises(ProductRoundtripError, match="handoff_create"):
        roundtrip(root, "legacy", tmp_path / "isolated", tmp_path / "workspace", CONTENT)
