from html.parser import HTMLParser
from pathlib import Path

import pytest

from server import openai_ui


ROOT = Path(__file__).parents[1]


def test_resource_metadata_is_read_only_and_has_no_network_permissions():
    resource = openai_ui.list_ui_resources()["resources"][0]
    assert resource["uri"] == openai_ui.UI_RESOURCE_URI
    assert resource["mimeType"] == "text/html;profile=mcp-app"
    assert resource["_meta"]["ui"]["csp"] == {
        "connectDomains": [], "resourceDomains": [], "frameDomains": [],
    }
    assert "permissions" not in resource["_meta"]["ui"]
    assert openai_ui.ui_tool_meta() == {
        "ui": {"resourceUri": resource["uri"], "visibility": ["model"]},
    }


def test_resource_read_returns_committed_self_contained_asset():
    result = openai_ui.read_ui_resource(openai_ui.UI_RESOURCE_URI)
    content = result["contents"][0]
    assert content["uri"] == openai_ui.UI_RESOURCE_URI
    assert content["mimeType"] == "text/html;profile=mcp-app"
    assert content["text"] == (ROOT / "assets/openai/read-only.html").read_text()
    assert all(line == line.rstrip(" \t") for line in content["text"].splitlines())
    assert "ui/initialize" in content["text"]  # Official bridge is bundled.
    assert "ui/notifications/tool-result" in content["text"]
    assert content["_meta"] == openai_ui.list_ui_resources()["resources"][0]["_meta"]


@pytest.mark.parametrize("uri", [
    "file:///etc/passwd", "ui://session-handoff/openai/../read-only.html",
    "ui://session-handoff/openai/read-only.html?path=/etc/passwd", "", None,
])
def test_resource_read_rejects_every_non_allowlisted_uri(uri):
    with pytest.raises(ValueError, match="unknown UI resource"):
        openai_ui.read_ui_resource(uri)


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.external = []
        self.script = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "link", "img", "iframe"}:
            self.external.extend(attributes[key] for key in ("src", "href") if key in attributes)
        self.script = tag == "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self.script = False

    def handle_data(self, data):
        if self.script:
            self.scripts.append(data)


def test_packaged_asset_has_no_remote_runtime_dependencies():
    parser = AssetParser()
    parser.feed(openai_ui.read_ui_resource(openai_ui.UI_RESOURCE_URI)["contents"][0]["text"])
    assert parser.external == []
    assert parser.scripts


def test_bundled_bridge_preserves_upstream_license_notices():
    notices = (ROOT / "assets/openai/THIRD_PARTY_LICENSES.txt").read_text()
    for name in ("@modelcontextprotocol/ext-apps", "@modelcontextprotocol/client", "@modelcontextprotocol/core", "zod"):
        assert name in notices
    assert "Apache License" in notices
    assert "Copyright (c) 2025 Colin McDonnell" in notices
