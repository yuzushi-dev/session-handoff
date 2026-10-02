"""Allowlisted, static MCP App resource; no workspace or session data is read."""

from pathlib import Path
from typing import Any


UI_RESOURCE_URI = "ui://session-handoff/openai/read-only.html"
UI_MIME_TYPE = "text/html;profile=mcp-app"
_ASSET = Path(__file__).resolve().parents[1] / "assets/openai/read-only.html"


def _resource_meta() -> dict[str, Any]:
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
            "prefersBorder": True,
        },
    }


def ui_tool_meta() -> dict[str, Any]:
    """Tool metadata: the model invokes the tool and the App only displays it."""
    return {"ui": {"resourceUri": UI_RESOURCE_URI, "visibility": ["model"]}}


def list_ui_resources() -> dict[str, Any]:
    return {
        "resources": [{
            "uri": UI_RESOURCE_URI,
            "name": "Codex thread status",
            "description": "Read-only view of the thread status tool result.",
            "mimeType": UI_MIME_TYPE,
            "_meta": _resource_meta(),
        }],
    }


def read_ui_resource(uri: str) -> dict[str, Any]:
    """Read only the exact known URI; caller maps ValueError to an MCP error."""
    if uri != UI_RESOURCE_URI:
        raise ValueError("unknown UI resource")
    return {
        "contents": [{
            "uri": UI_RESOURCE_URI,
            "mimeType": UI_MIME_TYPE,
            "text": _ASSET.read_text(encoding="utf-8"),
            "_meta": _resource_meta(),
        }],
    }
